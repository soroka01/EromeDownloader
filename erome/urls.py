import hashlib
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse, urlunparse

from erome.config import (
    ACCOUNT_EXCLUDED_PATHS,
    ACCOUNT_EXCLUDED_PREFIXES,
    ALBUM_URL_RE,
    EROME_HOSTS,
    MAX_PARALLEL_ALBUMS,
    MIN_CONNECTIONS_PER_PARALLEL_ALBUM,
    SORT_ALBUMS_BY_SIZE,
)
from erome.models import AlbumData, FileJob


def clean_queue_links(links: Iterable[str]) -> list[str]:
    return dedupe_preserve_order(
        stripped
        for stripped in (link.strip() for link in links if link)
        if stripped and not stripped.startswith("#")
    )


def _clean_album_title(title: str, default_title: str = "temp") -> str:
    title = re.sub(r'[\\/:*?"<>|]', "_", title)
    title = title.strip(". ")
    return title or default_title


def clean_input_url(url: str) -> str:
    return url.strip().strip("<>\"'").rstrip(".,;:")


def normalize_album_url(url: str) -> str:
    url = clean_input_url(url)
    match = ALBUM_URL_RE.search(url.strip())
    return clean_input_url(match.group(1)) if match else url


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen = set()
    unique_items = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique_items.append(item)
    return unique_items


def is_erome_album_url(url: str) -> bool:
    parsed = urlparse(clean_input_url(url))
    host = (parsed.hostname or "").lower()
    return host in EROME_HOSTS and parsed.path.startswith("/a/")


def is_erome_account_url(url: str) -> bool:
    parsed = urlparse(clean_input_url(url))
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if host not in EROME_HOSTS or not path or path == "/":
        return False
    if path in ACCOUNT_EXCLUDED_PATHS:
        return False
    if any(path.startswith(prefix) for prefix in ACCOUNT_EXCLUDED_PREFIXES):
        return False
    return "/" not in path.strip("/")


def normalize_account_url(url: str) -> str:
    url = clean_input_url(url)
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = urlparse(f"https://{url}")

    host = (parsed.hostname or "").lower()
    if host not in EROME_HOSTS:
        return url

    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", "www.erome.com", path, "", "", ""))


def normalize_download_url(url: str) -> str:
    url = clean_input_url(url)
    album_url = normalize_album_url(url)
    if is_erome_album_url(album_url):
        return album_url
    if is_erome_account_url(url):
        return normalize_account_url(url)
    return url


def link_kind(url: str) -> str:
    if is_erome_account_url(url):
        return "account"
    if is_erome_album_url(url):
        return "album"
    return "direct"


def download_sort_key(url: str) -> tuple[int, str, str]:
    kind_order = {"direct": 0, "album": 1, "account": 2}
    parsed = urlparse(url)
    return (
        kind_order.get(link_kind(url), 99),
        (parsed.hostname or "").lower(),
        parsed.path.lower(),
    )


def sort_download_links(links: Iterable[str]) -> list[str]:
    return sorted(links, key=download_sort_key)


def album_data_sort_key(album: AlbumData) -> tuple[int, int, int, int, int, str]:
    group = 0 if album.is_photo_only else 1
    if SORT_ALBUMS_BY_SIZE and album.has_full_size:
        return (
            0,
            album.size_bytes or 0,
            group,
            album.image_count,
            album.video_count,
            album.title.lower(),
        )
    if SORT_ALBUMS_BY_SIZE and album.size_bytes is not None:
        return (
            1,
            album.size_bytes,
            group,
            album.image_count,
            album.video_count,
            album.title.lower(),
        )
    return (
        2 if SORT_ALBUMS_BY_SIZE else group,
        group if SORT_ALBUMS_BY_SIZE else album.image_count,
        album.image_count if SORT_ALBUMS_BY_SIZE else album.video_count,
        album.video_count if SORT_ALBUMS_BY_SIZE else album.media_count,
        album.media_count,
        album.title.lower(),
    )


def sort_album_data(albums: Iterable[AlbumData]) -> list[AlbumData]:
    return sorted(albums, key=album_data_sort_key)


def prepare_account_links(links: Iterable[str]) -> list[str]:
    return dedupe_preserve_order(
        normalize_account_url(link)
        for link in clean_queue_links(links)
        if is_erome_account_url(normalize_download_url(link))
    )


def prepare_download_links(links: Iterable[str], sort_links: bool) -> list[str]:
    prepared = [
        normalize_download_url(link)
        for link in links
        if link.strip() and not link.strip().startswith("#")
    ]
    prepared = dedupe_preserve_order(prepared)
    return sort_download_links(prepared) if sort_links else prepared


def choose_parallel_album_count(item_count: int, max_connections: int) -> int:
    if item_count <= 1 or max_connections < MIN_CONNECTIONS_PER_PARALLEL_ALBUM * 2:
        return 1
    return min(
        item_count,
        MAX_PARALLEL_ALBUMS,
        max_connections // MIN_CONNECTIONS_PER_PARALLEL_ALBUM,
    )


def per_album_connection_limit(max_connections: int, parallel_albums: int) -> int:
    return max(1, max_connections // max(1, parallel_albums))


def is_finished_download_url(url: str, ready_urls: set[str], banned_urls: set[str]) -> bool:
    return not is_erome_account_url(url) and (url in ready_urls or url in banned_urls)


def account_name_from_url(url: str) -> str:
    parsed = urlparse(normalize_account_url(url))
    name = unquote(parsed.path.strip("/").split("/", 1)[0])
    return _clean_album_title(name, default_title="account")


def account_page_url(account_url: str, page: int) -> str:
    account_url = normalize_account_url(account_url)
    if page <= 1:
        return account_url
    return f"{account_url}?page={page}"


def safe_file_name_from_url(url: str, fallback_prefix: str = "file") -> str:
    parsed = urlparse(url)
    file_name = unquote(Path(parsed.path).name)
    if not file_name:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        file_name = f"{fallback_prefix}-{digest}.bin"
    return _clean_album_title(file_name, default_title=f"{fallback_prefix}.bin")


def build_download_jobs(urls: Iterable[str], download_path: Path) -> list[FileJob]:
    used_names: set[str] = set()
    jobs: list[FileJob] = []

    for url in dedupe_preserve_order(urls):
        file_name = safe_file_name_from_url(url)
        candidate = file_name
        index = 2

        while candidate.lower() in used_names:
            path = Path(file_name)
            candidate = f"{path.stem}_{index}{path.suffix}"
            index += 1

        used_names.add(candidate.lower())
        jobs.append(FileJob(url=url, path=download_path / candidate))

    return jobs
