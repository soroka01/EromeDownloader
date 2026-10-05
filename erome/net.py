import asyncio
import re
from contextlib import nullcontext
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
from aiohttp import ClientTimeout, TCPConnector
from bs4 import BeautifulSoup
from colorama import Fore
from tqdm import tqdm

from erome.config import (
    ACCOUNT_MAX_PAGES,
    ACCOUNT_PAGE_CONNECTIONS,
    ALBUM_PREFETCH_CONNECTIONS,
    ALBUM_SIZE_PROBE_CONNECTIONS,
    ALBUM_SIZE_PROBE_TIMEOUT,
    ALBUM_URL_RE,
    CONNECT_TIMEOUT,
    EROME_HOSTS,
    IDLE_TIMEOUT,
    PAGE_ATTEMPTS,
    RETRYABLE_STATUSES,
    UNAVAILABLE_STATUSES,
    USER_AGENT,
)
from erome.console import (
    format_bytes,
    friendly_error,
    log,
    log_retry_unless_quiet,
    log_warn,
    progress_options,
    short_url,
)
from erome.models import AlbumData, AlbumFetchError, DownloadError, DownloadResultCallback
from erome.urls import (
    _clean_album_title,
    account_name_from_url,
    account_page_url,
    dedupe_preserve_order,
    is_erome_album_url,
    normalize_account_url,
    normalize_album_url,
    sort_album_data,
)


def make_session(
    limit: int,
    headers: dict | None = None,
    timeout: ClientTimeout | None = None,
) -> aiohttp.ClientSession:
    limit = max(1, limit)
    if timeout is None:
        timeout = ClientTimeout(
            total=None,
            connect=CONNECT_TIMEOUT,
            sock_connect=CONNECT_TIMEOUT,
            sock_read=IDLE_TIMEOUT,
        )
    return aiohttp.ClientSession(
        connector=TCPConnector(limit=limit, limit_per_host=limit, ttl_dns_cache=300),
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        timeout=timeout,
    )


async def drain_tasks(tasks: list[asyncio.Task], on_result=None, progress=None) -> None:
    """Ждёт задачи по мере завершения; при ошибке отменяет остальные."""
    try:
        for task in asyncio.as_completed(tasks):
            result = await task
            if on_result:
                on_result(result)
            if progress is not None:
                progress.update(1)
    finally:
        for task in tasks:
            task.cancel()


def optional_progress(enabled: bool, **kwargs):
    return tqdm(**kwargs) if enabled else nullcontext()


def parse_content_length(headers) -> int:
    try:
        return int(headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def parse_content_range_total(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"/(\d+|\*)$", value.strip())
    if not match or match.group(1) == "*":
        return 0
    return int(match.group(1))


async def _probe_media_size(
    session: aiohttp.ClientSession,
    url: str,
    referer: str,
    semaphore: asyncio.Semaphore,
) -> int | None:
    async with semaphore:
        for method in ("HEAD", "GET"):
            headers = {"Referer": referer}
            if method == "GET":
                headers["Range"] = "bytes=0-0"
            try:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    allow_redirects=True,
                ) as response:
                    if response.status in UNAVAILABLE_STATUSES:
                        return None
                    if response.status in RETRYABLE_STATUSES:
                        continue
                    if method == "HEAD" and not response.ok:
                        continue
                    if method == "GET" and response.status not in {200, 206}:
                        continue

                    content_length = parse_content_length(response.headers)
                    if method == "HEAD":
                        if content_length:
                            return content_length
                        continue

                    total_size = parse_content_range_total(
                        response.headers.get("Content-Range")
                    )
                    if total_size:
                        return total_size
                    if response.status == 200 and content_length:
                        return content_length
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                continue
    return None


async def estimate_album_size(
    album: AlbumData,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> None:
    tasks = [
        asyncio.create_task(_probe_media_size(session, url, album.url, semaphore))
        for url in album.urls
    ]
    sizes = await asyncio.gather(*tasks)
    known_sizes = [size for size in sizes if size is not None]
    album.sized_media_count = len(known_sizes)
    album.unknown_size_count = len(sizes) - album.sized_media_count
    album.size_bytes = sum(known_sizes) if known_sizes else None


def album_size_text(album: AlbumData) -> str:
    if album.size_bytes is None:
        return "размер неизвестен"
    if album.unknown_size_count:
        return f">= {format_bytes(album.size_bytes)}"
    return format_bytes(album.size_bytes)


async def estimate_album_sizes_batch(
    albums: list[AlbumData],
    label: str,
    show_progress: bool = True,
) -> None:
    if not albums:
        return

    media_count = sum(album.media_count for album in albums)
    if not media_count:
        return

    probe_connections = max(
        1,
        min(ALBUM_SIZE_PROBE_CONNECTIONS, media_count),
    )
    probe_timeout = ClientTimeout(
        total=ALBUM_SIZE_PROBE_TIMEOUT,
        connect=min(CONNECT_TIMEOUT, ALBUM_SIZE_PROBE_TIMEOUT),
        sock_connect=min(CONNECT_TIMEOUT, ALBUM_SIZE_PROBE_TIMEOUT),
        sock_read=ALBUM_SIZE_PROBE_TIMEOUT,
    )
    semaphore = asyncio.Semaphore(probe_connections)

    log(
        f"{label}: оцениваю вес {len(albums)} альбомов "
        f"({media_count} файлов, {probe_connections} соединения)",
        Fore.CYAN,
        "SIZE",
    )
    async with make_session(
        probe_connections, {"Accept": "*/*"}, probe_timeout
    ) as session:
        tasks = [
            asyncio.create_task(estimate_album_size(album, session, semaphore))
            for album in albums
        ]
        with optional_progress(
            show_progress,
            total=len(tasks),
            desc=f"[{label}] size scan",
            unit="album",
            **progress_options("YELLOW", leave=False),
        ) as size_progress:
            await drain_tasks(tasks, progress=size_progress)

    full_count = sum(album.has_full_size for album in albums)
    partial_count = sum(
        album.size_bytes is not None and not album.has_full_size
        for album in albums
    )
    unknown_count = len(albums) - full_count - partial_count
    if full_count:
        smallest = min(
            (album for album in albums if album.has_full_size),
            key=lambda album: album.size_bytes or 0,
        )
        largest = max(
            (album for album in albums if album.has_full_size),
            key=lambda album: album.size_bytes or 0,
        )
        log(
            f"{label}: известен вес {full_count}/{len(albums)}, "
            f"частично {partial_count}, неизвестно {unknown_count}; "
            f"меньший {album_size_text(smallest)}, больший {album_size_text(largest)}",
            Fore.CYAN,
            "SIZE",
        )
    elif partial_count:
        log_warn(
            f"{label}: полный вес неизвестен, но есть частичные данные "
            f"для {partial_count}/{len(albums)}; сортирую по известному минимуму"
        )
    else:
        log_warn(
            f"{label}: сервер не отдал полный вес альбомов, "
            "оставляю старую сортировку"
        )


def parse_account_html(
    html_content: str,
    base_url: str,
    account_url: str,
) -> tuple[list[str], set[int]]:
    soup = BeautifulSoup(html_content, "html.parser")
    album_urls: list[str] = []
    page_numbers: set[int] = {1}
    account_path = urlparse(normalize_account_url(account_url)).path.rstrip("/")

    for link in soup.find_all("a", href=True):
        absolute_url = urljoin(base_url, link["href"])
        parsed = urlparse(absolute_url)
        host = (parsed.hostname or "").lower()

        if is_erome_album_url(absolute_url):
            album_urls.append(normalize_album_url(absolute_url))
            continue

        if host in EROME_HOSTS and parsed.path.rstrip("/") == account_path:
            for value in parse_qs(parsed.query).get("page", []):
                try:
                    page_number = int(value)
                except ValueError:
                    continue
                if page_number > 0:
                    page_numbers.add(page_number)

    for match in ALBUM_URL_RE.finditer(html_content):
        album_urls.append(normalize_album_url(match.group(1)))

    return dedupe_preserve_order(album_urls), page_numbers


async def _fetch_text_page(
    session: aiohttp.ClientSession,
    url: str,
    label: str,
) -> str:
    last_error = ""
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        try:
            async with session.get(url) as response:
                if response.status in UNAVAILABLE_STATUSES:
                    raise AlbumFetchError(f"HTTP {response.status}", response.status)
                if response.status in RETRYABLE_STATUSES:
                    raise DownloadError(f"HTTP {response.status}")
                if not response.ok:
                    raise AlbumFetchError(f"HTTP {response.status}", response.status)
                return await response.text(errors="replace")
        except AlbumFetchError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, DownloadError) as error:
            last_error = str(error) or error.__class__.__name__
            if attempt < PAGE_ATTEMPTS:
                delay = min(2 ** (attempt - 1), 8)
                log_retry_unless_quiet(
                    f"{label}: {friendly_error(last_error)} "
                    f"({attempt}/{PAGE_ATTEMPTS}), пауза {delay}s",
                    last_error,
                )
                await asyncio.sleep(delay)

    raise AlbumFetchError(last_error or f"не удалось получить страницу: {url}")


async def _collect_account_page_albums(
    session: aiohttp.ClientSession,
    page_url: str,
    account_url: str,
) -> tuple[str, list[str]] | AlbumFetchError:
    try:
        html_content = await _fetch_text_page(session, page_url, "account page")
    except AlbumFetchError as error:
        return error
    page_albums, _ = parse_account_html(
        html_content,
        base_url=page_url,
        account_url=account_url,
    )
    return page_url, page_albums


async def _collect_album_data_with_session(
    session: aiohttp.ClientSession,
    url: str,
    skip_videos: bool,
    skip_images: bool,
) -> AlbumData:
    html_content = await _fetch_text_page(session, url, "album page")
    return parse_album_html_data(
        html_content,
        base_url=url,
        album_url=url,
        skip_videos=skip_videos,
        skip_images=skip_images,
    )


async def collect_album_data_batch(
    urls: Iterable[str],
    skip_videos: bool,
    skip_images: bool,
    sort_albums: bool,
    label: str,
    estimate_sizes: bool = False,
    show_progress: bool = True,
    result_callback: DownloadResultCallback | None = None,
) -> tuple[list[AlbumData], list[tuple[str, str]]]:
    album_urls = list(dedupe_preserve_order(urls))
    if not album_urls:
        return [], []

    albums: list[AlbumData] = []
    failures: list[tuple[str, str]] = []

    async def collect_one(url: str) -> tuple[str, AlbumData | None, str | None]:
        try:
            album_data = await _collect_album_data_with_session(
                session,
                url,
                skip_videos,
                skip_images,
            )
            return url, album_data, None
        except AlbumFetchError as error:
            status = "banned" if error.status in UNAVAILABLE_STATUSES else "failed"
            return url, None, status

    def on_scanned(item: tuple[str, AlbumData | None, str | None]) -> None:
        url, album_data, status = item
        if album_data:
            albums.append(album_data)
        elif status:
            failures.append((url, status))
            if result_callback:
                result_callback(url, status)

    async with make_session(min(ALBUM_PREFETCH_CONNECTIONS, len(album_urls))) as session:
        tasks = [asyncio.create_task(collect_one(url)) for url in album_urls]
        with optional_progress(
            show_progress,
            total=len(tasks),
            desc=f"[{label}] album scan",
            unit="album",
            **progress_options("YELLOW", leave=False),
        ) as scan_progress:
            await drain_tasks(tasks, on_scanned, scan_progress)

    if failures:
        banned_count = sum(status == "banned" for _, status in failures)
        failed_count = sum(status == "failed" for _, status in failures)
        if banned_count:
            log_warn(
                f"{label}: недоступных альбомов {banned_count} "
                "(404/410), помечаю как banned"
            )
        if failed_count:
            log_warn(
                f"{label}: альбомов не прочитано {failed_count}; "
                "повторю позже, финально уйдёт в failed только после RETRY"
            )

    if estimate_sizes:
        await estimate_album_sizes_batch(albums, label, show_progress=show_progress)

    if sort_albums:
        albums = sort_album_data(albums)

    photo_only = sum(album.is_photo_only for album in albums)
    video_or_mixed = len(albums) - photo_only
    if albums:
        if estimate_sizes and any(album.size_bytes is not None for album in albums):
            log(
                f"{label}: сортировка по весу; "
                f"фото-only {photo_only}, с видео/смешанных {video_or_mixed}",
                Fore.CYAN,
                "SORT",
            )
        else:
            log(
                f"{label}: фото-only {photo_only}, "
                f"с видео/смешанных {video_or_mixed}",
                Fore.CYAN,
                "SORT",
            )

    return albums, failures


async def collect_account_album_urls(account_url: str) -> list[str]:
    account_url = normalize_account_url(account_url)
    async with make_session(ACCOUNT_PAGE_CONNECTIONS) as session:
        first_html = await _fetch_text_page(session, account_url, "account page")
        album_urls, page_numbers = parse_account_html(
            first_html,
            base_url=account_url,
            account_url=account_url,
        )

        max_page = max(page_numbers) if page_numbers else 1
        if max_page > ACCOUNT_MAX_PAGES:
            log_warn(
                f"{short_url(account_url)}: страниц больше {ACCOUNT_MAX_PAGES}, "
                "дальше ограничение безопасности"
            )
            max_page = ACCOUNT_MAX_PAGES

        page_urls = [account_page_url(account_url, page) for page in range(2, max_page + 1)]
        if not page_urls:
            return dedupe_preserve_order(album_urls)

        tasks = [
            asyncio.create_task(
                _collect_account_page_albums(session, page_url, account_url)
            )
            for page_url in page_urls
        ]
        def on_page(item: tuple[str, list[str]] | Exception) -> None:
            if isinstance(item, Exception):
                log_warn(f"account page: {friendly_error(item)}")
            else:
                album_urls.extend(item[1])

        with tqdm(
            total=len(tasks),
            desc=f"[{account_name_from_url(account_url)}] pages",
            unit="page",
            **progress_options("YELLOW", leave=False),
        ) as pages_progress:
            await drain_tasks(tasks, on_page, pages_progress)

    return dedupe_preserve_order(album_urls)


async def _collect_album_data(
    url: str,
    skip_videos: bool,
    skip_images: bool,
) -> AlbumData:
    async with make_session(2) as session:
        return await _collect_album_data_with_session(
            session, url, skip_videos, skip_images
        )


def parse_album_html_data(
    html_content: str,
    base_url: str,
    album_url: str,
    skip_videos: bool,
    skip_images: bool,
) -> AlbumData:
    soup = BeautifulSoup(html_content, "html.parser")

    meta_title = soup.find("meta", property="og:title")
    if meta_title and meta_title.has_attr("content"):
        album_title = _clean_album_title(meta_title["content"])
    else:
        album_title = _clean_album_title("temp")

    video_urls: list[str] = []
    if not skip_videos:
        for video_source in soup.find_all("source"):
            src = video_source.get("src")
            if src:
                video_urls.append(urljoin(base_url, src))

    image_urls: list[str] = []
    if not skip_images:
        for image in soup.find_all("img", {"class": "img-back"}):
            data_src = image.get("data-src") or image.get("src")
            if data_src:
                image_urls.append(urljoin(base_url, data_src))

    video_urls = dedupe_preserve_order(video_urls)
    image_urls = dedupe_preserve_order(image_urls)
    urls = dedupe_preserve_order([*video_urls, *image_urls])

    return AlbumData(
        url=normalize_album_url(album_url),
        title=album_title,
        urls=urls,
        video_urls=video_urls,
        image_urls=image_urls,
    )
