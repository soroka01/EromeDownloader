import asyncio
from typing import Callable, Iterable

from colorama import Fore
from tqdm import tqdm

from erome.config import (
    DOWNLOADS_DIR,
    GROUP_BAR_FORMAT,
    SORT_ALBUMS_BY_SIZE,
    UNAVAILABLE_STATUSES,
)
from erome.console import (
    DownloadSpeed,
    friendly_error,
    log,
    log_auto,
    log_done,
    log_error,
    log_queue,
    log_result_summary,
    log_retry,
    log_skip,
    log_success,
    log_warn,
    progress_options,
    refresh_speed_postfix,
    short_url,
    shorten_text,
)
from erome.downloader import _download
from erome.models import (
    AlbumData,
    AlbumFetchError,
    DownloadResult,
    DownloadResultCallback,
    FileResultCallback,
)
from erome.net import (
    _collect_album_data,
    album_size_text,
    collect_account_album_urls,
    collect_album_data_batch,
)
from erome.storage import (
    PendingQueue,
    _get_final_download_path,
    append_pending_links,
    read_pending,
    read_status_set,
    record_account_manifest,
    record_album_manifest,
    result_marker,
    set_account_status,
    set_download_status,
    set_status,
    write_pending,
)
from erome.urls import (
    account_name_from_url,
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


async def dump(
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
) -> str:
    url = normalize_download_url(url)
    if not url:
        return "failed"

    if is_erome_account_url(url):
        return await dump_account(
            account_url=url,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
        )

    if not is_erome_album_url(url):
        summary = await _download(
            album=url,
            urls=[url],
            max_connections=1,
            download_path=DOWNLOADS_DIR,
            desc="Direct file",
            show_progress=show_progress,
            show_summary=show_summary,
            byte_progress=byte_progress,
            file_result_callback=file_result_callback,
        )
        return summary.overall_status()

    try:
        album_data = await _collect_album_data(
            url=url,
            skip_videos=skip_videos,
            skip_images=skip_images,
        )
    except AlbumFetchError as error:
        if error.status in UNAVAILABLE_STATUSES:
            log_warn(f"{short_url(url)} недоступен ({error.status})")
            return "banned"
        log_error(f"{short_url(url)}: {friendly_error(error)}")
        return "failed"

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
) -> str:
    if not album_data.urls:
        log_warn(f"{short_url(album_data.url)}: медиа не найдены")
        return "failed"

    download_path = _get_final_download_path(album_data.title)
    if show_summary:
        size_info = (
            f", вес {album_size_text(album_data)}"
            if album_data.size_bytes is not None
            else ""
        )
        log(
            f"{shorten_text(album_data.title, 90)}: {album_data.media_count} файлов "
            f"({album_data.image_count} фото, {album_data.video_count} видео"
            f"{size_info})",
            Fore.CYAN,
            "ALBUM",
        )
    desc = f"[{album_idx}/{album_total}] Album" if album_idx else "Album"
    summary = await _download(
        album=album_data.url,
        urls=album_data.urls,
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


async def dump_account(
    account_url: str,
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
) -> str:
    account_url = normalize_account_url(account_url)
    account_name = account_name_from_url(account_url)

    try:
        album_urls = await collect_account_album_urls(account_url)
    except AlbumFetchError as error:
        if error.status in UNAVAILABLE_STATUSES:
            log_warn(f"{short_url(account_url)} недоступен ({error.status})")
            record_account_manifest(account_url, "banned")
            return "banned"
        log_error(f"{short_url(account_url)}: {friendly_error(error)}")
        record_account_manifest(account_url, "failed")
        return "failed"

    if not album_urls:
        log_warn(f"{short_url(account_url)}: посты не найдены")
        record_account_manifest(account_url, "failed", found=0)
        return "failed"

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
        f"уже обработано {len(skipped_urls)}",
        Fore.GREEN,
        "ACCOUNT",
    )

    if not pending_album_urls:
        record_account_manifest(
            account_url,
            "ready",
            found=len(album_urls),
            new=0,
        )
        return "success"

    mark_album_initial_result = result_marker(set_status, failed_is_final=False)
    mark_album_final_result = result_marker(set_status, failed_is_final=True)

    results = await download_links_parallel(
        urls=pending_album_urls,
        max_connections=max_connections,
        skip_videos=skip_videos,
        skip_images=skip_images,
        label=account_name,
        result_callback=mark_album_initial_result,
    )

    banned_urls = [
        album_url
        for album_url, result in results
        if result == "banned"
    ]
    failed_urls = [
        album_url
        for album_url, result in results
        if result == "failed"
    ]

    if failed_urls:
        log_retry(f"{account_name}: повторная попытка для {len(failed_urls)} постов")
        retry_failed: list[str] = []
        retry_results = await download_links_parallel(
            urls=failed_urls,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
            label=f"{account_name} retry",
            result_callback=mark_album_final_result,
        )
        for album_url, result in retry_results:
            if result == "banned":
                banned_urls.append(album_url)
            elif result == "failed":
                retry_failed.append(album_url)
        failed_urls = retry_failed

    if failed_urls:
        log_warn(
            f"{account_name}: failed={len(failed_urls)}, banned={len(banned_urls)}"
        )
        record_account_manifest(
            account_url,
            "failed",
            found=len(album_urls),
            new=len(pending_album_urls),
        )
        return "failed"

    record_account_manifest(
        account_url,
        "ready",
        found=len(album_urls),
        new=len(pending_album_urls),
    )
    return "success"


async def download_url_group_parallel(
    urls: list[str],
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    label: str,
    result_callback: DownloadResultCallback | None = None,
) -> list[tuple[str, str]]:
    if not urls:
        return []

    parallel_items = min(len(urls), max_connections)
    total = len(urls)
    show_item_progress = total == 1 and parallel_items == 1
    speed = DownloadSpeed()
    log_queue(f"{label}: {total} ссылок")
    if parallel_items > 1:
        log_auto(
            f"{label}: {parallel_items} файлов параллельно "
            f"(общий лимит {max_connections})"
        )

    semaphore = asyncio.Semaphore(parallel_items)

    async def run_one(index: int, url: str) -> tuple[str, str]:
        async with semaphore:
            result = await dump(
                url,
                1,
                skip_videos,
                skip_images,
                album_idx=index,
                album_total=total,
                show_progress=show_item_progress,
                show_summary=show_item_progress,
                byte_progress=None if show_item_progress else speed.add,
            )
            return url, result

    tasks = [
        asyncio.create_task(run_one(index, url))
        for index, url in enumerate(urls, 1)
    ]
    results: list[tuple[str, str]] = []
    if show_item_progress:
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            if result_callback:
                result_callback(*result)
    else:
        with tqdm(
            total=total,
            desc=label,
            unit="file",
            **progress_options("MAGENTA", leave=True, bar_format=GROUP_BAR_FORMAT),
        ) as group_progress:
            stop_speed = asyncio.Event()
            speed_task = asyncio.create_task(
                refresh_speed_postfix(group_progress, speed, stop_speed)
            )
            try:
                for task in asyncio.as_completed(tasks):
                    result = await task
                    results.append(result)
                    if result_callback:
                        result_callback(*result)
                    group_progress.update(1)
            finally:
                stop_speed.set()
                await speed_task
    log_result_summary(label, results)
    return results


async def download_album_group_parallel(
    albums: list[AlbumData],
    max_connections: int,
    label: str,
    photo_mode: bool,
    result_callback: DownloadResultCallback | None = None,
) -> list[tuple[str, str]]:
    if not albums:
        return []

    if photo_mode:
        parallel_albums = min(len(albums), max_connections)
    else:
        parallel_albums = choose_parallel_album_count(len(albums), max_connections)

    per_album_connections = per_album_connection_limit(max_connections, parallel_albums)
    total = len(albums)
    show_album_progress = total == 1
    speed = DownloadSpeed()
    photo_only_total = sum(album.is_photo_only for album in albums)
    photo_only_remaining = photo_only_total
    total_files = sum(album.media_count for album in albums)
    album_by_url = {album.url: album for album in albums}
    file_progress_ref: dict[str, tqdm | None] = {"bar": None}
    album_file_done: dict[str, int] = {album.url: 0 for album in albums}

    photo_text = (
        f", фото-only {photo_only_total}"
        if photo_only_total
        else ""
    )
    log_queue(
        f"{label}: {total} альбомов{photo_text}, "
        f"{total_files} файлов, по {per_album_connections} соединения на альбом"
    )
    if parallel_albums > 1:
        log_auto(
            f"{label}: {parallel_albums} альбомов параллельно, "
            f"по {per_album_connections} соединения на альбом "
            f"(общий лимит {max_connections})"
        )

    semaphore = asyncio.Semaphore(parallel_albums)

    async def run_one(index: int, album_data: AlbumData) -> tuple[str, str]:
        async with semaphore:
            def on_file_done(_: DownloadResult) -> None:
                file_progress = file_progress_ref["bar"]
                if file_progress is None:
                    return
                album_file_done[album_data.url] += 1
                done = album_file_done[album_data.url]
                file_progress.set_postfix_str(
                    f"{done}/{album_data.media_count} "
                    f"{shorten_text(album_data.title, 30)}",
                    refresh=False,
                )
                file_progress.update(1)

            result = await download_album_data(
                album_data=album_data,
                max_connections=per_album_connections,
                album_idx=index,
                album_total=total,
                show_progress=show_album_progress,
                show_summary=show_album_progress,
                byte_progress=None if show_album_progress else speed.add,
                file_result_callback=None if show_album_progress else on_file_done,
            )
            return album_data.url, result

    results: list[tuple[str, str]] = []
    if show_album_progress:
        tasks = [
            asyncio.create_task(run_one(index, album_data))
            for index, album_data in enumerate(albums, 1)
        ]
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            if result_callback:
                result_callback(*result)
    else:
        def album_extra_status() -> str:
            if not photo_only_total:
                return ""
            done = photo_only_total - photo_only_remaining
            return f"фото-only {done}/{photo_only_total}, осталось {photo_only_remaining}"

        with tqdm(
            total=total,
            desc=label,
            unit="album",
            **progress_options(
                "MAGENTA",
                leave=True,
                position=0,
                bar_format=GROUP_BAR_FORMAT,
            ),
        ) as group_progress, tqdm(
            total=total_files,
            desc=f"{label} files",
            unit="file",
            **progress_options(
                "CYAN",
                leave=True,
                position=1,
                bar_format=GROUP_BAR_FORMAT,
            ),
        ) as files_progress:
            file_progress_ref["bar"] = files_progress
            tasks = [
                asyncio.create_task(run_one(index, album_data))
                for index, album_data in enumerate(albums, 1)
            ]
            stop_speed = asyncio.Event()
            speed_task = asyncio.create_task(
                refresh_speed_postfix(
                    group_progress,
                    speed,
                    stop_speed,
                    extra_status=album_extra_status,
                )
            )
            try:
                for task in asyncio.as_completed(tasks):
                    result = await task
                    results.append(result)
                    if result_callback:
                        result_callback(*result)
                    album = album_by_url.get(result[0])
                    if album and album.is_photo_only:
                        photo_only_remaining -= 1
                    group_progress.update(1)
            finally:
                file_progress_ref["bar"] = None
                stop_speed.set()
                await speed_task
    log_result_summary(label, results)
    return results


async def download_links_parallel(
    urls: Iterable[str],
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    label: str,
    result_callback: DownloadResultCallback | None = None,
) -> list[tuple[str, str]]:
    urls = list(urls)
    if not urls:
        return []

    results: list[tuple[str, str]] = []

    direct_urls = [url for url in urls if link_kind(url) == "direct"]
    album_urls = [url for url in urls if link_kind(url) == "album"]
    fallback_urls = [
        url for url in urls if link_kind(url) not in {"direct", "album"}
    ]

    album_task = None
    if album_urls:
        if direct_urls and SORT_ALBUMS_BY_SIZE:
            log(
                f"{label}: в фоне готовлю {len(album_urls)} альбомов "
                "к сортировке по весу",
                Fore.CYAN,
                "SIZE",
            )
        album_task = asyncio.create_task(
            collect_album_data_batch(
                album_urls,
                skip_videos=skip_videos,
                skip_images=skip_images,
                sort_albums=True,
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

    if fallback_urls:
        results.extend(
            await download_url_group_parallel(
                urls=fallback_urls,
                max_connections=max_connections,
                skip_videos=skip_videos,
                skip_images=skip_images,
                label=f"{label} other",
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
        log(
            "Ссылки отсортированы: файлы -> фото-альбомы -> видео -> аккаунты.",
            Fore.CYAN,
            "SORT",
        )

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
        log_skip(f"Уже обработано: {len(skipped_links)}")

    if not work_links:
        log_done("Новых ссылок нет.")
        write_pending([])
        return

    pending_queue = PendingQueue(work_links)
    pending_queue.flush()
    log(
        f"К скачиванию: {len(regular_links)} ссылок, "
        f"аккаунтов: {len(account_links)}",
        Fore.GREEN,
        "START",
    )

    def forget_pending(url: str) -> None:
        pending_queue.remove(url)

    mark_initial_result = result_marker(
        set_download_status, failed_is_final=False, after=forget_pending
    )
    mark_final_result = result_marker(
        set_download_status, failed_is_final=True, after=forget_pending
    )

    regular_results = await download_links_parallel(
        urls=regular_links,
        max_connections=max_connections,
        skip_videos=skip_videos,
        skip_images=skip_images,
        label="BATCH",
        result_callback=mark_initial_result,
    )

    failed_urls = [
        url
        for url, result in regular_results
        if result == "failed"
    ]

    if failed_urls:
        log_retry(f"Повторная попытка: {len(failed_urls)}")
        retry_results = await download_links_parallel(
            urls=failed_urls,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
            label="RETRY",
            result_callback=mark_final_result,
        )
        failed_urls = [
            url
            for url, result in retry_results
            if result == "failed"
        ]

    for index, url in enumerate(account_links, 1):
        log(f"{index}/{len(account_links)} {short_url(url)}", Fore.CYAN, "ACCOUNT")
        result = await dump_account(
            account_url=url,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
        )
        mark_final_result(url, result)

    pending_queue.flush()


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
        log("Аккаунты отсортированы.", Fore.CYAN, "SORT")

    if not account_links:
        log_warn("В links/accs.txt нет аккаунтов для отслеживания.")
        return

    log(f"Проверяем {len(account_links)} аккаунтов", Fore.GREEN, "START")

    for index, account_url in enumerate(account_links, 1):
        log(
            f"{index}/{len(account_links)} {short_url(account_url)}",
            Fore.CYAN,
            "ACCOUNT",
        )
        result = await dump_account(
            account_url=account_url,
            max_connections=max_connections,
            skip_videos=skip_videos,
            skip_images=skip_images,
        )

        if result == "success":
            set_account_status(account_url, "ready")
        elif result == "banned":
            set_account_status(account_url, "banned")
        else:
            set_account_status(account_url, "failed")


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
        account_name = account_name_from_url(account_url)
        log(f"[{index}/{len(account_links)}] {account_name}: сбор постов")
        try:
            album_urls = await collect_account_album_urls(account_url)
        except AlbumFetchError as error:
            status = "banned" if error.status in UNAVAILABLE_STATUSES else "failed"
            set_account_status(account_url, status)
            record_account_manifest(account_url, status)
            log_error(f"{account_name}: {friendly_error(error)}")
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
