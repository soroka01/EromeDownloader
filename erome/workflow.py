import asyncio
from typing import Awaitable, Callable, Iterable

from erome.config import DOWNLOADS_DIR, SORT_ALBUMS_BY_SIZE
from erome.console import (
    LiveProgress,
    checkpoint,
    friendly_error,
    log,
    log_error,
    log_hint,
    log_result_summary,
    log_retry,
    log_success,
    log_warn,
    short_url,
    shorten_text,
    stop_requested,
)
from erome.downloader import download_jobs
from erome.manifest import record_account_manifest, record_album_manifest
from erome.models import (
    AlbumData,
    AlbumFetchError,
    DownloadResult,
    DownloadResultCallback,
    FileResultCallback,
    LinkKind,
    Outcome,
)
from erome.net import (
    album_size_text,
    collect_account_album_urls,
    collect_album_data,
    collect_album_data_batch,
)
from erome.storage import (
    PendingQueue,
    album_download_dir,
    append_pending_links,
    read_pending,
    read_status_set,
    result_marker,
    set_account_status,
    set_download_status,
    set_status,
    write_pending,
)
from erome.urls import (
    account_name_from_url,
    build_download_jobs,
    choose_parallel_album_count,
    is_erome_account_url,
    is_erome_album_url,
    is_finished_download_url,
    link_kind,
    normalize_account_url,
    normalize_download_url,
    per_album_connection_limit,
    prepare_account_links,
    prepare_download_links,
    sort_download_links,
)


def unexpected_failure(url: str, error: Exception) -> Outcome:
    """Log an error that no handler expected and return the result status of the failed item."""
    detail = friendly_error(error)
    name = type(error).__name__
    log_error(f"{short_url(url)}: {detail if detail.startswith(name) else f'{name}: {detail}'}")
    return Outcome.FAILED


async def download_link(
    url: str,
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    album_idx: int | None = None,
    album_total: int | None = None,
    show_progress: bool = True,
    show_summary: bool = True,
    byte_progress: Callable[[int], None] | None = None,
    file_result_callback: FileResultCallback | None = None,
) -> Outcome:
    url = normalize_download_url(url)
    if not url:
        return Outcome.FAILED

    if is_erome_account_url(url):
        return await download_account(
            account_url=url,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
        )

    if not is_erome_album_url(url):
        summary = await download_jobs(
            album=url,
            jobs=build_download_jobs([url], DOWNLOADS_DIR),
            max_connections=1,
            download_path=DOWNLOADS_DIR,
            desc="Файл",
            show_progress=show_progress,
            show_summary=show_summary,
            byte_progress=byte_progress,
            file_result_callback=file_result_callback,
        )
        return summary.overall_status()

    try:
        album_data = await collect_album_data(
            url=url,
            skip_videos=skip_videos,
            skip_images=skip_images,
        )
    except AlbumFetchError as error:
        if error.unavailable:
            log_warn(f"{short_url(url)} недоступен ({error.status})")
            return Outcome.BANNED
        log_error(f"{short_url(url)}: {friendly_error(error)}")
        return Outcome.FAILED

    return await download_album_data(
        album_data=album_data,
        max_connections=max_connections,
        album_idx=album_idx,
        album_total=album_total,
        show_progress=show_progress,
        show_summary=show_summary,
        byte_progress=byte_progress,
        file_result_callback=file_result_callback,
    )


async def download_album_data(
    album_data: AlbumData,
    max_connections: int,
    album_idx: int | None = None,
    album_total: int | None = None,
    show_progress: bool = True,
    show_summary: bool = True,
    byte_progress: Callable[[int], None] | None = None,
    file_result_callback: FileResultCallback | None = None,
) -> Outcome:
    if not album_data.urls:
        log_warn(f"{short_url(album_data.url)}: медиа не найдены")
        return Outcome.FAILED

    download_path = album_download_dir(album_data.title)
    if show_summary:
        size_info = (
            f", вес {album_size_text(album_data)}"
            if album_data.size_bytes is not None
            else ""
        )
        log(
            f"{shorten_text(album_data.title, 90)}: {album_data.media_count} файлов "
            f"({album_data.image_count} фото, {album_data.video_count} видео"
            f"{size_info})"
        )
    desc = f"[{album_idx}/{album_total}] Альбом" if album_idx else "Альбом"
    summary = await download_jobs(
        album=album_data.url,
        jobs=build_download_jobs(album_data.urls, download_path),
        max_connections=max_connections,
        download_path=download_path,
        desc=desc,
        show_progress=show_progress,
        show_summary=show_summary,
        byte_progress=byte_progress,
        file_result_callback=file_result_callback,
    )
    status = summary.overall_status()
    record_album_manifest(album_data, download_path, summary, status)
    return status


async def run_with_retry(
    urls: list[str],
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    label: str,
    retry_label: str,
    retry_message: Callable[[int], str],
    initial_callback: DownloadResultCallback,
    final_callback: DownloadResultCallback,
) -> tuple[list[str], list[str]]:
    """Download the links and repeat the failed ones once. Returns (banned, failed)."""

    async def run(batch: list[str], batch_label: str, callback: DownloadResultCallback):
        return await download_links_parallel(
            urls=batch,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
            label=batch_label,
            result_callback=callback,
        )

    results = await run(urls, label, initial_callback)
    banned = [url for url, result in results if result == Outcome.BANNED]
    failed = [url for url, result in results if result == Outcome.FAILED]

    if failed and not stop_requested():
        log_retry(retry_message(len(failed)))
        retry_results = await run(failed, retry_label, final_callback)
        banned += [url for url, result in retry_results if result == Outcome.BANNED]
        failed = [url for url, result in retry_results if result == Outcome.FAILED]

    return banned, failed


async def download_account(
    account_url: str,
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
) -> Outcome:
    account_url = normalize_account_url(account_url)
    account_name = account_name_from_url(account_url)

    try:
        album_urls = await collect_account_album_urls(account_url)
    except AlbumFetchError as error:
        if error.unavailable:
            log_warn(f"{short_url(account_url)} недоступен ({error.status})")
            record_account_manifest(account_url, "banned")
            return Outcome.BANNED
        log_error(f"{short_url(account_url)}: {friendly_error(error)}")
        record_account_manifest(account_url, "failed")
        return Outcome.FAILED

    if not album_urls:
        log_warn(f"{short_url(account_url)}: посты не найдены")
        record_account_manifest(account_url, "failed", found=0)
        return Outcome.FAILED

    ready_albums = read_status_set("ready")
    banned_albums = read_status_set("banned")
    skipped_urls = [url for url in album_urls if url in ready_albums or url in banned_albums]
    pending_album_urls = [
        url
        for url in album_urls
        if url not in ready_albums and url not in banned_albums
    ]

    log(
        f"{account_name}: найдено {len(album_urls)} постов, "
        f"новых к скачиванию {len(pending_album_urls)}, "
        f"уже обработано {len(skipped_urls)}"
    )

    if not pending_album_urls:
        record_account_manifest(
            account_url,
            "ready",
            found=len(album_urls),
            new=0,
        )
        return Outcome.SUCCESS

    mark_album_initial_result = result_marker(set_status, failed_is_final=False)
    mark_album_final_result = result_marker(set_status, failed_is_final=True)

    banned_urls, failed_urls = await run_with_retry(
        pending_album_urls,
        max_connections,
        skip_videos,
        skip_images,
        label=account_name,
        retry_label=f"{account_name} retry",
        retry_message=lambda count: f"{account_name}: повторная попытка для {count} постов",
        initial_callback=mark_album_initial_result,
        final_callback=mark_album_final_result,
    )

    if stop_requested():
        return Outcome.STOPPED

    if failed_urls:
        log_warn(
            f"{account_name}: ошибок {len(failed_urls)}, недоступно {len(banned_urls)}"
        )
        record_account_manifest(
            account_url,
            "failed",
            found=len(album_urls),
            new=len(pending_album_urls),
        )
        return Outcome.FAILED

    record_account_manifest(
        account_url,
        "ready",
        found=len(album_urls),
        new=len(pending_album_urls),
    )
    return Outcome.SUCCESS


async def download_account_safely(
    account_url: str,
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
) -> Outcome:
    """`download_account` for loops over several accounts: an unexpected error fails this account only."""
    try:
        return await download_account(
            account_url=account_url,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
        )
    except Exception as error:
        return unexpected_failure(account_url, error)


async def run_group[T](
    items: list[T],
    *,
    key: Callable[[T], str],
    worker: Callable[[int, T], Awaitable[Outcome]],
    parallel: int,
    progress: LiveProgress,
    single: bool,
    label: str,
    result_callback: DownloadResultCallback | None,
    on_result: Callable[[str], None] | None = None,
) -> list[tuple[str, Outcome]]:
    """Run `worker(index, item)` over the items, `parallel` at a time, and report how each ended.

    The user's pause and stop requests are honoured between items; an item that was not started because of a stop
    is left out of the results (it stays in its queue). An item that fails unexpectedly is reported and does not
    stop the others. With a `single` item its own progress is drawn instead of the group line.
    """
    semaphore = asyncio.Semaphore(parallel)

    async def run_one(index: int, item: T) -> tuple[str, Outcome]:
        url = key(item)
        async with semaphore:
            if await checkpoint():
                return url, Outcome.STOPPED
            try:
                return url, await worker(index, item)
            except Exception as error:  # one broken item must not stop the whole batch
                return url, unexpected_failure(url, error)

    results: list[tuple[str, Outcome]] = []

    def accept(outcome: tuple[str, Outcome]) -> None:
        url, status = outcome
        if status == Outcome.STOPPED:
            return  # not started: stays in the queue for the next run
        results.append(outcome)
        if result_callback:
            result_callback(url, status)
        if on_result:
            on_result(url)

    tasks = [asyncio.create_task(run_one(index, item)) for index, item in enumerate(items, 1)]
    if single:
        for task in asyncio.as_completed(tasks):
            accept(await task)
    else:
        with progress:
            for task in asyncio.as_completed(tasks):
                accept(await task)
                progress.update(1)
    log_result_summary(label, results)
    return results


async def download_url_group_parallel(
    urls: list[str],
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    label: str,
    result_callback: DownloadResultCallback | None = None,
) -> list[tuple[str, Outcome]]:
    if not urls or stop_requested():
        return []

    total = len(urls)
    parallel_items = min(total, max_connections)
    single = total == 1
    progress = LiveProgress(label, total=total, unit="ссылок")
    log_hint(f"{label}: {total} ссылок")
    if parallel_items > 1:
        log_hint(f"{label}: {parallel_items} файлов параллельно (общий лимит {max_connections})")

    async def download(index: int, url: str) -> Outcome:
        return await download_link(
            url,
            1,
            skip_videos,
            skip_images,
            album_idx=index,
            album_total=total,
            show_progress=single,
            show_summary=single,
            byte_progress=None if single else progress.add_bytes,
        )

    return await run_group(
        urls,
        key=str,
        worker=download,
        parallel=parallel_items,
        progress=progress,
        single=single,
        label=label,
        result_callback=result_callback,
    )


async def download_album_group_parallel(
    albums: list[AlbumData],
    max_connections: int,
    label: str,
    photo_mode: bool,
    result_callback: DownloadResultCallback | None = None,
) -> list[tuple[str, Outcome]]:
    if not albums or stop_requested():
        return []

    if photo_mode:
        parallel_albums = min(len(albums), max_connections)
    else:
        parallel_albums = choose_parallel_album_count(len(albums), max_connections)

    per_album_connections = per_album_connection_limit(max_connections, parallel_albums)
    total = len(albums)
    single = total == 1
    photo_only_total = sum(album.is_photo_only for album in albums)
    photo_only_remaining = photo_only_total
    total_files = sum(album.media_count for album in albums)
    album_by_url = {album.url: album for album in albums}
    files_done = {album.url: 0 for album in albums}
    progress = LiveProgress(
        label,
        total=total,
        unit="альбомов",
        total_bytes=(
            sum(album.size_bytes or 0 for album in albums)
            if all(album.has_full_size for album in albums)
            else 0
        ),
    )
    progress.files_total = total_files

    photo_text = f", фото-only {photo_only_total}" if photo_only_total else ""
    log_hint(
        f"{label}: {total} альбомов{photo_text}, "
        f"{total_files} файлов, по {per_album_connections} соединения на альбом"
    )
    if parallel_albums > 1:
        log_hint(
            f"{label}: {parallel_albums} альбомов параллельно, "
            f"по {per_album_connections} соединения на альбом "
            f"(общий лимит {max_connections})"
        )

    async def download(index: int, album: AlbumData) -> Outcome:
        def on_file_done(_: DownloadResult) -> None:
            files_done[album.url] += 1
            progress.note = f"{files_done[album.url]}/{album.media_count} {shorten_text(album.title, 30)}"
            progress.add_file()

        return await download_album_data(
            album_data=album,
            max_connections=per_album_connections,
            album_idx=index,
            album_total=total,
            show_progress=single,
            show_summary=single,
            byte_progress=None if single else progress.add_bytes,
            file_result_callback=None if single else on_file_done,
        )

    def album_finished(url: str) -> None:
        nonlocal photo_only_remaining
        if album_by_url[url].is_photo_only:
            photo_only_remaining -= 1

    def photo_only_status() -> str:
        if not photo_only_total:
            return ""
        done = photo_only_total - photo_only_remaining
        return f"фото-only {done}/{photo_only_total}, осталось {photo_only_remaining}"

    if not single:
        progress.extra = photo_only_status
    return await run_group(
        albums,
        key=lambda album: album.url,
        worker=download,
        parallel=parallel_albums,
        progress=progress,
        single=single,
        label=label,
        result_callback=result_callback,
        on_result=album_finished,
    )


async def download_links_parallel(
    urls: Iterable[str],
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    label: str,
    result_callback: DownloadResultCallback | None = None,
) -> list[tuple[str, Outcome]]:
    urls = list(urls)
    if not urls:
        return []

    results: list[tuple[str, Outcome]] = []

    album_urls = [url for url in urls if link_kind(url) == LinkKind.ALBUM]
    direct_urls = [url for url in urls if link_kind(url) != LinkKind.ALBUM]  # files; download_link also copes with a profile

    album_task = None
    if album_urls:
        if direct_urls and SORT_ALBUMS_BY_SIZE:
            log_hint(
                f"{label}: в фоне готовлю {len(album_urls)} альбомов "
                "к сортировке по весу"
            )
        album_task = asyncio.create_task(
            collect_album_data_batch(
                album_urls,
                skip_videos=skip_videos,
                skip_images=skip_images,
                label=label,
                estimate_sizes=SORT_ALBUMS_BY_SIZE,
                show_progress=not direct_urls,
                result_callback=result_callback,
            )
        )

    if direct_urls:
        results.extend(
            await download_url_group_parallel(
                urls=direct_urls,
                max_connections=max_connections,
                skip_videos=skip_videos,
                skip_images=skip_images,
                label=f"{label} files",
                result_callback=result_callback,
            )
        )

    if album_task:
        albums, scan_failures = await album_task
        results.extend(scan_failures)
    else:
        albums = []

    has_size_order = SORT_ALBUMS_BY_SIZE and any(
        album.size_bytes is not None for album in albums
    )
    if has_size_order:
        results.extend(
            await download_album_group_parallel(
                albums=albums,
                max_connections=max_connections,
                label=f"{label} ALBUMS",
                photo_mode=False,
                result_callback=result_callback,
            )
        )
    else:
        photo_albums = [album for album in albums if album.is_photo_only]
        video_albums = [album for album in albums if not album.is_photo_only]

        if photo_albums:
            results.extend(
                await download_album_group_parallel(
                    albums=photo_albums,
                    max_connections=max_connections,
                    label=f"{label} PHOTO",
                    photo_mode=True,
                    result_callback=result_callback,
                )
            )

        if video_albums:
            results.extend(
                await download_album_group_parallel(
                    albums=video_albums,
                    max_connections=max_connections,
                    label=f"{label} VIDEO",
                    photo_mode=False,
                    result_callback=result_callback,
                )
            )

    return results


async def batch_download(
    links: Iterable[str],
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    sort_links: bool,
) -> None:
    valid_links = prepare_download_links(links, sort_links=sort_links)

    if sort_links:
        log_hint("Ссылки отсортированы: файлы -> фото-альбомы -> видео -> аккаунты.")

    if not valid_links:
        log_warn("В links/pending.txt нет ссылок для скачивания.")
        write_pending([])
        return

    ready_urls = read_status_set("ready")
    banned_urls = read_status_set("banned")
    skipped_links = [
        url
        for url in valid_links
        if is_finished_download_url(url, ready_urls, banned_urls)
    ]
    work_links = [
        url
        for url in valid_links
        if not is_finished_download_url(url, ready_urls, banned_urls)
    ]
    regular_links = [url for url in work_links if not is_erome_account_url(url)]
    account_links = [url for url in work_links if is_erome_account_url(url)]

    if skipped_links:
        log_hint(f"Уже обработано: {len(skipped_links)}")

    if not work_links:
        log_success("Новых ссылок нет.")
        write_pending([])
        return

    pending_queue = PendingQueue(work_links)
    pending_queue.flush()
    log(
        f"К скачиванию: {len(regular_links)} ссылок, "
        f"аккаунтов: {len(account_links)}"
    )

    def forget_pending(url: str) -> None:
        pending_queue.remove(url)

    mark_initial_result = result_marker(
        set_download_status, failed_is_final=False, after=forget_pending
    )
    mark_final_result = result_marker(
        set_download_status, failed_is_final=True, after=forget_pending
    )

    await run_with_retry(
        regular_links,
        max_connections,
        skip_videos,
        skip_images,
        label="BATCH",
        retry_label="RETRY",
        retry_message=lambda count: f"Повторная попытка: {count}",
        initial_callback=mark_initial_result,
        final_callback=mark_final_result,
    )

    for index, url in enumerate(account_links, 1):
        if await checkpoint():
            break
        log(f"Аккаунт {index}/{len(account_links)}: {short_url(url)}")
        result = await download_account_safely(url, max_connections, skip_videos, skip_images)
        if result == Outcome.STOPPED:
            break
        mark_final_result(url, result)

    pending_queue.flush()
    if stop_requested():
        log_warn("Остановлено по запросу: необработанное осталось в links/pending.txt")


async def batch_download_accounts(
    links: Iterable[str],
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    sort_links: bool,
) -> None:
    account_links = prepare_account_links(links)

    if sort_links:
        account_links = sort_download_links(account_links)
        log_hint("Аккаунты отсортированы.")

    if not account_links:
        log_warn("В links/accs.txt нет аккаунтов для отслеживания.")
        return

    log(f"Проверяем {len(account_links)} аккаунтов")
    mark_result = result_marker(set_account_status, failed_is_final=True)

    for index, account_url in enumerate(account_links, 1):
        if await checkpoint():
            log_warn("Остановлено по запросу: остальные аккаунты не проверены")
            break
        log(f"Аккаунт {index}/{len(account_links)}: {short_url(account_url)}")
        result = await download_account_safely(account_url, max_connections, skip_videos, skip_images)
        if result == Outcome.STOPPED:
            log_warn("Остановлено по запросу: аккаунт будет проверен при следующем запуске")
            break
        mark_result(account_url, result)


async def scan_accounts_to_pending(
    links: Iterable[str],
    sort_links: bool,
) -> None:
    account_links = prepare_account_links(links)
    if sort_links:
        account_links = sort_download_links(account_links)

    if not account_links:
        log_warn("В links/accs.txt нет аккаунтов для проверки.")
        return

    ready_urls = read_status_set("ready")
    banned_urls = read_status_set("banned")
    failed_urls = read_status_set("failed")
    pending_urls = set(read_pending())
    added_total = 0

    log(f"Проверяю аккаунты без скачивания: {len(account_links)}")

    for index, account_url in enumerate(account_links, 1):
        if await checkpoint():
            log_warn("Остановлено по запросу: остальные аккаунты не проверены")
            break
        account_name = account_name_from_url(account_url)
        log(f"[{index}/{len(account_links)}] {account_name}: сбор постов")
        try:
            album_urls = await collect_account_album_urls(account_url)
        except AlbumFetchError as error:
            status = error.outcome
            set_account_status(account_url, status)
            record_account_manifest(account_url, status)
            log_error(f"{account_name}: {friendly_error(error)}")
            continue
        except Exception as error:
            unexpected_failure(account_url, error)
            set_account_status(account_url, "failed")
            record_account_manifest(account_url, "failed")
            continue

        known_urls = ready_urls | banned_urls | failed_urls | pending_urls
        new_urls = [url for url in album_urls if url not in known_urls]
        if sort_links:
            new_urls = sort_download_links(new_urls)

        added = append_pending_links(new_urls)
        pending_urls.update(new_urls)
        added_total += added
        set_account_status(account_url, "ready")
        record_account_manifest(
            account_url,
            "ready",
            found=len(album_urls),
            new=len(new_urls),
            added_to_pending=added,
        )
        log_success(
            f"{account_name}: найдено {len(album_urls)}, новых {len(new_urls)}, "
            f"добавлено в pending {added}"
        )

    log_success(f"Проверка аккаунтов завершена. Добавлено новых альбомов: {added_total}")
