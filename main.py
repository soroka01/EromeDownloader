import asyncio
import hashlib
import json
import re
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout, TCPConnector
from bs4 import BeautifulSoup
from colorama import Fore, Style, init
from tqdm import tqdm


BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
LINKS_DIR = BASE_DIR / "links"
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "max_connections": 6,
    "min_connections_per_parallel_album": 3,
    "max_parallel_albums": 3,
    "auto_sort_links": True,
    "album_prefetch_connections": 6,
    "account_page_connections": 3,
    "account_max_pages": 500,
    "sort_albums_by_size": True,
    "album_size_probe_connections": 3,
    "album_size_probe_timeout": 20,
    "chunk_size_mb": 1,
    "max_attempts": 4,
    "page_attempts": 3,
    "connect_timeout": 30,
    "idle_timeout": 180,
    "skip_videos": False,
    "skip_images": False,
    "manifest_path": "links/manifest.json",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return DEFAULT_CONFIG.copy()

    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"[WARN] config.json повреждён: {error}. Использую настройки по умолчанию.")
        return DEFAULT_CONFIG.copy()

    config = DEFAULT_CONFIG.copy()
    if isinstance(loaded, dict):
        config.update(loaded)
    return config


CONFIG = load_config()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
EROME_HOSTS = {"erome.com", "www.erome.com"}
ALBUM_URL_RE = re.compile(
    r"(https?://(?:www\.)?erome\.com/a/[^\s\"'<>/?#]+)",
    re.IGNORECASE,
)
ACCOUNT_EXCLUDED_PREFIXES = (
    "/a/",
    "/about",
    "/contact",
    "/explore",
    "/latest",
    "/popular",
    "/privacy",
    "/search",
    "/terms",
    "/user/",
)
ACCOUNT_EXCLUDED_PATHS = {"/a"}

CHUNK_SIZE = max(1, int(CONFIG["chunk_size_mb"])) * 1024 * 1024
MAX_ATTEMPTS = int(CONFIG["max_attempts"])
PAGE_ATTEMPTS = int(CONFIG["page_attempts"])
ACCOUNT_PAGE_CONNECTIONS = int(CONFIG["account_page_connections"])
ACCOUNT_MAX_PAGES = int(CONFIG["account_max_pages"])
DEFAULT_MAX_CONNECTIONS = int(CONFIG["max_connections"])
MIN_CONNECTIONS_PER_PARALLEL_ALBUM = int(CONFIG["min_connections_per_parallel_album"])
MAX_PARALLEL_ALBUMS = int(CONFIG["max_parallel_albums"])
AUTO_SORT_LINKS = bool(CONFIG["auto_sort_links"])
ALBUM_PREFETCH_CONNECTIONS = int(CONFIG["album_prefetch_connections"])
SORT_ALBUMS_BY_SIZE = bool(CONFIG["sort_albums_by_size"])
ALBUM_SIZE_PROBE_CONNECTIONS = int(CONFIG["album_size_probe_connections"])
ALBUM_SIZE_PROBE_TIMEOUT = int(CONFIG["album_size_probe_timeout"])
CONNECT_TIMEOUT = int(CONFIG["connect_timeout"])
IDLE_TIMEOUT = int(CONFIG["idle_timeout"])
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 522, 524}
UNAVAILABLE_STATUSES = {403, 404, 410}
STATUS_FILES = ("ready", "failed", "banned")
ACCOUNT_STATUS_FILES = ("ready_accs", "failed_accs", "banned_accs")
BAR_FORMAT = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]"
GROUP_BAR_FORMAT = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}, {rate_fmt}{postfix}]"
MAX_PROGRESS_WIDTH = 112
MAX_LOG_MESSAGE = 180
DownloadResultCallback = Callable[[str, str], None]
FileResultCallback = Callable[["DownloadResult"], None]


@dataclass(slots=True)
class FileJob:
    url: str
    path: Path


@dataclass(slots=True)
class DownloadResult:
    url: str
    status: str
    path: Path | None = None
    error: str = ""
    attempts: int = 0
    size_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {"success", "skipped"}


@dataclass(slots=True)
class DownloadSummary:
    results: list[DownloadResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok_count(self) -> int:
        return sum(result.ok for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status == "failed" for result in self.results)

    @property
    def banned_count(self) -> int:
        return sum(result.status == "banned" for result in self.results)

    @property
    def total_size(self) -> int:
        return sum(result.size_bytes for result in self.results if result.ok)

    def overall_status(self) -> str:
        if not self.results:
            return "failed"
        if self.failed_count == 0 and self.banned_count == 0:
            return "success"
        if self.ok_count == 0 and self.banned_count > 0:
            return "banned"
        return "failed"


@dataclass(slots=True)
class AlbumData:
    url: str
    title: str
    urls: list[str]
    video_urls: list[str]
    image_urls: list[str]
    size_bytes: int | None = None
    sized_media_count: int = 0
    unknown_size_count: int = 0

    @property
    def video_count(self) -> int:
        return len(self.video_urls)

    @property
    def image_count(self) -> int:
        return len(self.image_urls)

    @property
    def media_count(self) -> int:
        return len(self.urls)

    @property
    def is_photo_only(self) -> bool:
        return self.image_count > 0 and self.video_count == 0

    @property
    def has_full_size(self) -> bool:
        return self.size_bytes is not None and self.unknown_size_count == 0


class DownloadError(Exception):
    pass


class AlbumFetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    init(autoreset=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_bytes(size: int | float) -> str:
    size = float(size or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def format_speed(bytes_per_second: int | float) -> str:
    speed = max(0.0, float(bytes_per_second or 0))
    if speed < 1024 * 1024:
        return f"{speed / 1024:.1f} KB/s"
    if speed < 1024 * 1024 * 1024:
        return f"{speed / 1024 / 1024:.2f} MB/s"
    return f"{speed / 1024 / 1024 / 1024:.2f} GB/s"


class DownloadSpeed:
    def __init__(self, window_seconds: float = 5.0):
        self.window_seconds = window_seconds
        self.samples: deque[tuple[float, int]] = deque()

    def add(self, byte_count: int) -> None:
        if byte_count <= 0:
            return
        now = time.monotonic()
        self.samples.append((now, byte_count))
        self._trim(now)

    def rate(self) -> float:
        now = time.monotonic()
        self._trim(now)
        if not self.samples:
            return 0.0
        elapsed = max(now - self.samples[0][0], 0.5)
        return sum(byte_count for _, byte_count in self.samples) / elapsed

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()


async def refresh_speed_postfix(
    progress: tqdm,
    speed: DownloadSpeed,
    stop_event: asyncio.Event,
    extra_status: Callable[[], str] | None = None,
) -> None:
    def postfix_text() -> str:
        parts = [format_speed(speed.rate())]
        if extra_status:
            extra = extra_status()
            if extra:
                parts.append(extra)
        return ", ".join(parts)

    while not stop_event.is_set():
        progress.set_postfix_str(postfix_text(), refresh=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
    progress.set_postfix_str(postfix_text(), refresh=True)


def relative_path(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return str(path.resolve().relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def shorten_text(value: object, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def short_url(url: str, limit: int = 110) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        path = unquote(parsed.path.rstrip("/"))
        tail = Path(path).name
        if tail:
            text = f"{parsed.netloc}/.../{tail}"
        else:
            text = parsed.netloc
        if parsed.query:
            text += "?..."
        return shorten_text(text, limit)
    return shorten_text(url, limit)


def friendly_error(error: object) -> str:
    message = str(error) or error.__class__.__name__
    if (
        "ContentLengthError" in message
        or "Response payload is not completed" in message
        or "Not enough data to satisfy content length header" in message
    ):
        return "соединение оборвалось до конца файла"
    if is_timeout_error(message):
        return "таймаут соединения"
    return shorten_text(message, MAX_LOG_MESSAGE)


def is_timeout_error(error: object) -> bool:
    message = str(error) or error.__class__.__name__
    normalized = message.lower()
    return (
        "timeout" in normalized
        or "timed out" in normalized
        or "нет данных слишком долго" in normalized
    )


def retryable_http_status(error: object) -> int | None:
    message = str(error) or error.__class__.__name__
    match = re.search(r"\bHTTP\s+(\d{3})\b", message, re.IGNORECASE)
    if not match:
        return None
    status = int(match.group(1))
    return status if status in RETRYABLE_STATUSES else None


def is_quiet_retry_error(error: object) -> bool:
    return is_timeout_error(error) or retryable_http_status(error) is not None


def log_retry_unless_quiet(message: str, error: object) -> None:
    if not is_quiet_retry_error(error):
        log_retry(message)


def progress_ncols() -> int:
    columns = shutil.get_terminal_size((100, 20)).columns
    return max(60, min(columns, MAX_PROGRESS_WIDTH))


def progress_options(
    colour: str,
    leave: bool = False,
    position: int | None = None,
    bar_format: str = BAR_FORMAT,
) -> dict:
    options = {
        "colour": colour,
        "leave": leave,
        "ascii": True,
        "ncols": progress_ncols(),
        "bar_format": bar_format,
    }
    if position is not None:
        options["position"] = position
    return options


def log(message: str, color=Fore.WHITE, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    tag = level.upper()[:7].ljust(7)
    tqdm.write(color + f"[{timestamp}] {tag} {message}" + Style.RESET_ALL)


def log_success(message: str) -> None:
    log(message, Fore.GREEN, "OK")


def log_warn(message: str) -> None:
    log(message, Fore.YELLOW, "WARN")


def log_error(message: str) -> None:
    log(message, Fore.RED, "ERROR")


def log_retry(message: str) -> None:
    log(message, Fore.YELLOW, "RETRY")


def log_auto(message: str) -> None:
    log(message, Fore.CYAN, "AUTO")


def log_skip(message: str) -> None:
    log(message, Fore.GREEN, "SKIP")


def log_queue(message: str) -> None:
    log(message, Fore.CYAN, "QUEUE")


def log_done(message: str, color=Fore.GREEN) -> None:
    log(message, color, "DONE")


def log_result_summary(label: str, results: list[tuple[str, str]]) -> None:
    if not results:
        return
    total = len(results)
    ok_count = sum(result == "success" for _, result in results)
    banned_count = sum(result == "banned" for _, result in results)
    failed_count = total - ok_count - banned_count
    if failed_count or banned_count:
        log_warn(
            f"{label}: готово {ok_count}/{total}, "
            f"ошибок {failed_count}, недоступно {banned_count}"
        )
    else:
        log_success(f"{label}: готово {ok_count}/{total}")


def print_boxed(text: str, color=Fore.CYAN) -> None:
    lines = text.split("\n")
    width = max(len(line) for line in lines) + 2
    print(color + "┌" + "─" * width + "┐")
    for line in lines:
        print(color + "│ " + line.ljust(width - 2) + " │")
    print(color + "└" + "─" * width + "┘" + Style.RESET_ALL)


def ensure_runtime_dirs() -> None:
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    LINKS_DIR.mkdir(exist_ok=True)


def resolve_base_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def manifest_file_path() -> Path:
    ensure_runtime_dirs()
    path = resolve_base_path(str(CONFIG["manifest_path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def empty_manifest() -> dict:
    return {
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "files": {},
        "albums": {},
        "accounts": {},
    }


def load_manifest() -> dict:
    path = manifest_file_path()
    if not path.exists():
        return empty_manifest()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        bad_path = path.with_suffix(path.suffix + ".bad")
        path.replace(bad_path)
        log_warn(f"Manifest повреждён, старый файл перенесён в {relative_path(bad_path)}")
        return empty_manifest()

    if not isinstance(manifest, dict):
        return empty_manifest()

    manifest.setdefault("version", 1)
    manifest.setdefault("created_at", now_iso())
    manifest.setdefault("files", {})
    manifest.setdefault("albums", {})
    manifest.setdefault("accounts", {})
    manifest["updated_at"] = now_iso()
    return manifest


def save_manifest(manifest: dict) -> None:
    path = manifest_file_path()
    manifest["updated_at"] = now_iso()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def record_file_results(
    referrer_url: str,
    download_path: Path,
    results: Iterable[DownloadResult],
) -> None:
    manifest = load_manifest()
    files = manifest.setdefault("files", {})
    for result in results:
        files[result.url] = {
            "url": result.url,
            "referrer_url": referrer_url,
            "folder": relative_path(download_path),
            "path": relative_path(result.path),
            "size_bytes": result.size_bytes,
            "size": format_bytes(result.size_bytes),
            "status": result.status,
            "attempts": result.attempts,
            "error": result.error,
            "updated_at": now_iso(),
        }
    save_manifest(manifest)


def record_album_manifest(
    album_data: AlbumData,
    download_path: Path,
    summary: DownloadSummary,
    status: str,
) -> None:
    manifest = load_manifest()
    manifest.setdefault("albums", {})[album_data.url] = {
        "url": album_data.url,
        "title": album_data.title,
        "folder": relative_path(download_path),
        "size_bytes": summary.total_size,
        "size": format_bytes(summary.total_size),
        "status": status,
        "files_total": summary.total,
        "files_ok": summary.ok_count,
        "files_failed": summary.failed_count,
        "files_banned": summary.banned_count,
        "images": album_data.image_count,
        "videos": album_data.video_count,
        "updated_at": now_iso(),
    }
    save_manifest(manifest)


def record_account_manifest(
    account_url: str,
    status: str,
    found: int = 0,
    new: int = 0,
    added_to_pending: int = 0,
) -> None:
    manifest = load_manifest()
    manifest.setdefault("accounts", {})[account_url] = {
        "url": account_url,
        "status": status,
        "albums_found": found,
        "albums_new": new,
        "added_to_pending": added_to_pending,
        "updated_at": now_iso(),
    }
    save_manifest(manifest)


def status_file_path(name: str) -> Path:
    ensure_runtime_dirs()
    return LINKS_DIR / f"{name}.txt"


def read_clean_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def add_status(url: str, status: str) -> None:
    path = status_file_path(status)
    lines = read_clean_lines(path)
    if url not in set(lines):
        write_queue(status, [*lines, url])


def remove_status(url: str, status: str) -> None:
    path = status_file_path(status)
    lines = [line for line in read_clean_lines(path) if line != url]
    write_queue(status, lines)


def set_status(url: str, status: str) -> None:
    for name in STATUS_FILES:
        if name != status:
            remove_status(url, name)
    add_status(url, status)


def set_account_status(url: str, status: str) -> None:
    status_file = f"{status}_accs"
    for name in ACCOUNT_STATUS_FILES:
        if name != status_file:
            remove_status(url, name)
    add_status(url, status_file)


def set_download_status(url: str, status: str) -> None:
    if is_erome_account_url(url):
        set_account_status(url, status)
    else:
        set_status(url, status)


def read_status_set(status: str) -> set[str]:
    return set(read_clean_lines(status_file_path(status)))


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def write_queue(name: str, links: Iterable[str]) -> None:
    path = status_file_path(name)
    clean_links = [
        link.strip()
        for link in links
        if link and link.strip() and not link.strip().startswith("#")
    ]
    clean_links = dedupe_preserve_order(clean_links)
    write_text_atomic(
        path,
        "\n".join(clean_links) + ("\n" if clean_links else ""),
    )


def read_queue(name: str) -> list[str]:
    path = status_file_path(name)
    if not path.exists():
        path.touch()
        return []
    return read_clean_lines(path)


def write_pending(links: Iterable[str]) -> None:
    write_queue("pending", links)


def read_pending() -> list[str]:
    return read_queue("pending")


def append_pending_links(links: Iterable[str]) -> int:
    current = read_pending()
    current_set = set(current)
    additions = []
    for link in links:
        normalized = normalize_download_url(link)
        if normalized and normalized not in current_set:
            additions.append(normalized)
            current_set.add(normalized)
    additions = dedupe_preserve_order(additions)
    if additions:
        write_pending([*current, *additions])
    return len(additions)


class PendingQueue:
    def __init__(self, links: Iterable[str]):
        self.remaining = dedupe_preserve_order(
            normalize_download_url(link)
            for link in links
            if link and link.strip()
        )
        self.remaining_set = set(self.remaining)

    def flush(self) -> None:
        write_pending(self.remaining)

    def remove(self, url: str) -> None:
        normalized = normalize_download_url(url)
        if normalized not in self.remaining_set:
            return
        self.remaining_set.remove(normalized)
        self.remaining = [link for link in self.remaining if link != normalized]
        self.flush()


def read_accounts() -> list[str]:
    return read_queue("accs")


def _clean_album_title(title: str, default_title: str = "temp") -> str:
    title = re.sub(r'[\\/:*?"<>|]', "_", title)
    title = title.strip(". ")
    return title or default_title


def _get_final_download_path(album_title: str) -> Path:
    final_path = DOWNLOADS_DIR / _clean_album_title(album_title)
    final_path.mkdir(parents=True, exist_ok=True)
    return final_path


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
    timeout = ClientTimeout(
        total=ALBUM_SIZE_PROBE_TIMEOUT,
        connect=min(CONNECT_TIMEOUT, ALBUM_SIZE_PROBE_TIMEOUT),
        sock_connect=min(CONNECT_TIMEOUT, ALBUM_SIZE_PROBE_TIMEOUT),
        sock_read=ALBUM_SIZE_PROBE_TIMEOUT,
    )
    connector = TCPConnector(
        limit=probe_connections,
        limit_per_host=probe_connections,
        ttl_dns_cache=300,
    )
    headers = {
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
    }
    semaphore = asyncio.Semaphore(probe_connections)

    log(
        f"{label}: оцениваю вес {len(albums)} альбомов "
        f"({media_count} файлов, {probe_connections} соединения)",
        Fore.CYAN,
        "SIZE",
    )
    async with aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=timeout,
    ) as session:
        tasks = [
            asyncio.create_task(estimate_album_size(album, session, semaphore))
            for album in albums
        ]
        if show_progress:
            with tqdm(
                total=len(tasks),
                desc=f"[{label}] size scan",
                unit="album",
                **progress_options("YELLOW", leave=False),
            ) as size_progress:
                for task in asyncio.as_completed(tasks):
                    await task
                    size_progress.update(1)
        else:
            for task in asyncio.as_completed(tasks):
                await task

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
) -> tuple[str, list[str]]:
    html_content = await _fetch_text_page(session, page_url, "account page")
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

    timeout = ClientTimeout(
        total=None,
        connect=CONNECT_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=IDLE_TIMEOUT,
    )
    connector = TCPConnector(
        limit=min(ALBUM_PREFETCH_CONNECTIONS, len(album_urls)),
        limit_per_host=min(ALBUM_PREFETCH_CONNECTIONS, len(album_urls)),
        ttl_dns_cache=300,
    )
    headers = {"User-Agent": USER_AGENT}
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

    async with aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=timeout,
    ) as session:
        tasks = [asyncio.create_task(collect_one(url)) for url in album_urls]
        if show_progress:
            with tqdm(
                total=len(tasks),
                desc=f"[{label}] album scan",
                unit="album",
                **progress_options("YELLOW", leave=False),
            ) as scan_progress:
                for task in asyncio.as_completed(tasks):
                    url, album_data, status = await task
                    if album_data:
                        albums.append(album_data)
                    elif status:
                        failures.append((url, status))
                        if result_callback:
                            result_callback(url, status)
                    scan_progress.update(1)
        else:
            for task in asyncio.as_completed(tasks):
                url, album_data, status = await task
                if album_data:
                    albums.append(album_data)
                elif status:
                    failures.append((url, status))
                    if result_callback:
                        result_callback(url, status)

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
    timeout = ClientTimeout(
        total=None,
        connect=CONNECT_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=IDLE_TIMEOUT,
    )
    connector = TCPConnector(
        limit=ACCOUNT_PAGE_CONNECTIONS,
        limit_per_host=ACCOUNT_PAGE_CONNECTIONS,
        ttl_dns_cache=300,
    )
    headers = {"User-Agent": USER_AGENT}

    async with aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=timeout,
    ) as session:
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
        with tqdm(
            total=len(tasks),
            desc=f"[{account_name_from_url(account_url)}] pages",
            unit="page",
            **progress_options("YELLOW", leave=False),
        ) as pages_progress:
            for task in asyncio.as_completed(tasks):
                try:
                    _, page_albums = await task
                    album_urls.extend(page_albums)
                except AlbumFetchError as error:
                    log_warn(f"account page: {friendly_error(error)}")
                finally:
                    pages_progress.update(1)

    return dedupe_preserve_order(album_urls)


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

    def mark_album_result(album_url: str, result: str, failed_is_final: bool) -> None:
        if result == "success":
            set_status(album_url, "ready")
        elif result == "banned":
            set_status(album_url, "banned")
        elif failed_is_final:
            set_status(album_url, "failed")

    def mark_album_initial_result(album_url: str, result: str) -> None:
        mark_album_result(album_url, result, failed_is_final=False)

    def mark_album_final_result(album_url: str, result: str) -> None:
        mark_album_result(album_url, result, failed_is_final=True)

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


async def _download(
    album: str,
    urls: Iterable[str],
    max_connections: int,
    download_path: Path,
    desc: str = "Album",
    show_progress: bool = True,
    show_summary: bool = True,
    byte_progress: Callable[[int], None] | None = None,
    file_result_callback: FileResultCallback | None = None,
) -> DownloadSummary:
    ensure_runtime_dirs()
    download_path.mkdir(parents=True, exist_ok=True)

    jobs = build_download_jobs(urls, download_path)
    if not jobs:
        return DownloadSummary(results=[])

    max_connections = max(1, min(max_connections, len(jobs), 32))
    timeout = ClientTimeout(
        total=None,
        connect=CONNECT_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=IDLE_TIMEOUT,
    )
    connector = TCPConnector(
        limit=max_connections,
        limit_per_host=max_connections,
        ttl_dns_cache=300,
    )
    headers = {
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Referer": album,
        "User-Agent": USER_AGENT,
    }

    results: list[DownloadResult] = []
    semaphore = asyncio.Semaphore(max_connections)
    progress_slots: asyncio.Queue[int] | None = None
    if show_progress:
        progress_slots = asyncio.Queue()
        for position in range(1, max_connections + 1):
            progress_slots.put_nowait(position)

    async with aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=timeout,
    ) as session:
        tasks = [
            asyncio.create_task(
                _download_file(
                    session,
                    job,
                    semaphore,
                    show_progress=show_progress,
                    progress_slots=progress_slots,
                    show_messages=show_summary,
                    byte_progress=byte_progress,
                )
            )
            for job in jobs
        ]
        if show_progress:
            with tqdm(
                total=len(tasks),
                desc=desc,
                unit="file",
                **progress_options("MAGENTA", leave=True, position=0),
            ) as album_progress:
                for task in asyncio.as_completed(tasks):
                    result = await task
                    results.append(result)
                    if file_result_callback:
                        file_result_callback(result)
                    album_progress.update(1)
        else:
            for task in asyncio.as_completed(tasks):
                result = await task
                results.append(result)
                if file_result_callback:
                    file_result_callback(result)

    summary = DownloadSummary(results=results)
    record_file_results(album, download_path, results)
    if show_summary:
        if summary.failed_count or summary.banned_count:
            log_warn(
                f"ok={summary.ok_count}/{summary.total}, "
                f"failed={summary.failed_count}, "
                f"banned={summary.banned_count}, "
                f"size={format_bytes(summary.total_size)}"
            )
        else:
            log_success(
                f"{download_path.name}: {summary.ok_count}/{summary.total} файлов, "
                f"{format_bytes(summary.total_size)}"
            )
    return summary


async def _download_file(
    session: aiohttp.ClientSession,
    job: FileJob,
    semaphore: asyncio.Semaphore,
    show_progress: bool,
    progress_slots: asyncio.Queue[int] | None,
    show_messages: bool,
    byte_progress: Callable[[int], None] | None,
) -> DownloadResult:
    part_path = job.path.with_name(job.path.name + ".part")
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with semaphore:
                progress_position = None
                if show_progress and progress_slots is not None:
                    progress_position = await progress_slots.get()
                try:
                    return await _download_file_once(
                        session,
                        job,
                        part_path,
                        attempt,
                        show_progress=show_progress,
                        progress_position=progress_position,
                        show_messages=show_messages,
                        byte_progress=byte_progress,
                    )
                finally:
                    if progress_position is not None and progress_slots is not None:
                        progress_slots.put_nowait(progress_position)
        except DownloadError as error:
            last_error = str(error)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            last_error = str(error) or error.__class__.__name__

        if attempt < MAX_ATTEMPTS:
            delay = min(2 ** (attempt - 1), 12)
            log_retry_unless_quiet(
                f"{job.path.name}: {friendly_error(last_error)} "
                f"({attempt}/{MAX_ATTEMPTS}), пауза {delay}s",
                last_error,
            )
            await asyncio.sleep(delay)

    if show_messages and not is_quiet_retry_error(last_error):
        log_error(f"{short_url(job.url)}: {friendly_error(last_error)}")
    return DownloadResult(
        url=job.url,
        status="failed",
        path=job.path,
        error=last_error,
        attempts=MAX_ATTEMPTS,
    )


async def _download_file_once(
    session: aiohttp.ClientSession,
    job: FileJob,
    part_path: Path,
    attempt: int,
    show_progress: bool,
    progress_position: int | None,
    show_messages: bool,
    byte_progress: Callable[[int], None] | None,
) -> DownloadResult:
    resume_from = part_path.stat().st_size if part_path.exists() else 0
    request_headers = {}
    if resume_from:
        request_headers["Range"] = f"bytes={resume_from}-"

    async with session.get(job.url, headers=request_headers) as response:
        if response.status in UNAVAILABLE_STATUSES:
            return DownloadResult(
                url=job.url,
                status="banned",
                path=job.path,
                error=f"HTTP {response.status}",
                attempts=attempt,
            )

        if response.status == 416:
            total_size = parse_content_range_total(response.headers.get("Content-Range"))
            if resume_from and total_size and resume_from >= total_size:
                part_path.replace(job.path)
                size = job.path.stat().st_size if job.path.exists() else total_size
                if show_messages:
                    log_success(f"{job.path.name} докачан ранее ({format_bytes(size)})")
                return DownloadResult(
                    job.url,
                    "success",
                    job.path,
                    attempts=attempt,
                    size_bytes=size,
                )
            if part_path.exists():
                part_path.unlink()
            raise DownloadError("сервер отклонил докачку, начинаю заново")

        if response.status in RETRYABLE_STATUSES:
            raise DownloadError(f"HTTP {response.status}")

        if not response.ok and response.status != 206:
            return DownloadResult(
                url=job.url,
                status="failed",
                path=job.path,
                error=f"HTTP {response.status}",
                attempts=attempt,
            )

        remote_total = parse_content_range_total(response.headers.get("Content-Range"))
        content_length = parse_content_length(response.headers)

        if resume_from and response.status == 206:
            mode = "ab"
            initial_size = resume_from
            total_size = remote_total or resume_from + content_length
        else:
            if resume_from and response.status == 200:
                if show_messages:
                    log(f"{job.path.name}: сервер не дал докачку, перекачиваю")
            mode = "wb"
            initial_size = 0
            total_size = content_length

        if job.path.exists() and total_size and job.path.stat().st_size == total_size:
            if part_path.exists():
                part_path.unlink()
            if show_messages:
                log_success(f"{job.path.name} уже скачан ({format_bytes(total_size)})")
            return DownloadResult(
                job.url,
                "skipped",
                job.path,
                attempts=attempt,
                size_bytes=total_size,
            )

        progress_total = total_size or None
        progress = None
        if show_progress:
            progress = tqdm(
                desc=shorten_text(f"[{job.path.parent.name}] {job.path.name}", 56),
                total=progress_total,
                initial=initial_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                **progress_options(
                    "CYAN",
                    leave=False,
                    position=progress_position,
                ),
            )
        try:
            last_data_time = time.monotonic()
            async with aiofiles.open(part_path, mode) as file:
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    now = time.monotonic()
                    if now - last_data_time > IDLE_TIMEOUT:
                        raise asyncio.TimeoutError("нет данных слишком долго")
                    if not chunk:
                        continue
                    await file.write(chunk)
                    last_data_time = now
                    if byte_progress is not None:
                        byte_progress(len(chunk))
                    if progress is not None:
                        progress.update(len(chunk))
        finally:
            if progress is not None:
                progress.close()

        downloaded_size = part_path.stat().st_size if part_path.exists() else 0
        if total_size and downloaded_size < total_size:
            raise DownloadError(
                f"файл неполный: {downloaded_size}/{total_size} байт"
            )

        part_path.replace(job.path)
        final_size = job.path.stat().st_size if job.path.exists() else downloaded_size
        return DownloadResult(
            job.url,
            "success",
            job.path,
            attempts=attempt,
            size_bytes=final_size,
        )


async def _collect_album_data(
    url: str,
    skip_videos: bool,
    skip_images: bool,
) -> AlbumData:
    timeout = ClientTimeout(
        total=None,
        connect=CONNECT_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=IDLE_TIMEOUT,
    )
    connector = TCPConnector(limit=2, limit_per_host=2, ttl_dns_cache=300)
    headers = {"User-Agent": USER_AGENT}
    last_error = ""

    async with aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=timeout,
    ) as session:
        for attempt in range(1, PAGE_ATTEMPTS + 1):
            try:
                async with session.get(url) as response:
                    if response.status in UNAVAILABLE_STATUSES:
                        raise AlbumFetchError(
                            f"HTTP {response.status}",
                            status=response.status,
                        )
                    if response.status in RETRYABLE_STATUSES:
                        raise DownloadError(f"HTTP {response.status}")
                    if not response.ok:
                        raise AlbumFetchError(
                            f"HTTP {response.status}",
                            status=response.status,
                        )

                    html_content = await response.text(errors="replace")
                    return parse_album_html_data(
                        html_content,
                        base_url=url,
                        album_url=url,
                        skip_videos=skip_videos,
                        skip_images=skip_images,
                    )
            except AlbumFetchError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, DownloadError) as error:
                last_error = str(error) or error.__class__.__name__
                if attempt < PAGE_ATTEMPTS:
                    delay = min(2 ** (attempt - 1), 8)
                    log_retry_unless_quiet(
                        f"album page: {friendly_error(last_error)} "
                        f"({attempt}/{PAGE_ATTEMPTS}), пауза {delay}s",
                        last_error,
                    )
                    await asyncio.sleep(delay)

    raise AlbumFetchError(last_error or "не удалось получить страницу альбома")


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


def parse_album_html(
    html_content: str,
    base_url: str,
    skip_videos: bool,
    skip_images: bool,
) -> tuple[str, list[str]]:
    album_data = parse_album_html_data(
        html_content,
        base_url=base_url,
        album_url=base_url,
        skip_videos=skip_videos,
        skip_images=skip_images,
    )
    return album_data.title, album_data.urls


def ask_mode() -> str:
    while True:
        mode = input(Fore.GREEN + "\nРежим [1-4]: " + Style.RESET_ALL).strip()
        if mode in {"1", "2", "3", "4"}:
            return mode
        log_error("Введите 1, 2, 3 или 4.")


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

    def mark_result(url: str, result: str, failed_is_final: bool) -> None:
        if result == "success":
            set_download_status(url, "ready")
            pending_queue.remove(url)
        elif result == "banned":
            set_download_status(url, "banned")
            pending_queue.remove(url)
        elif failed_is_final:
            set_download_status(url, "failed")
            pending_queue.remove(url)

    def mark_initial_result(url: str, result: str) -> None:
        mark_result(url, result, failed_is_final=False)

    def mark_final_result(url: str, result: str) -> None:
        mark_result(url, result, failed_is_final=True)

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
    account_links = [
        normalize_account_url(link)
        for link in links
        if link.strip()
        and not link.strip().startswith("#")
        and is_erome_account_url(normalize_download_url(link))
    ]
    account_links = dedupe_preserve_order(account_links)

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
    account_links = [
        normalize_account_url(link)
        for link in links
        if link.strip()
        and not link.strip().startswith("#")
        and is_erome_account_url(normalize_download_url(link))
    ]
    account_links = dedupe_preserve_order(account_links)
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


def main() -> None:
    configure_console()
    ensure_runtime_dirs()

    print_boxed("EromeDownloader\nby github.com/soroka01", Fore.MAGENTA)
    print(Fore.YELLOW + "\nДобро пожаловать. Выберите режим работы.")
    print(Fore.GREEN + "\nРежимы:")
    print(
        Fore.CYAN
        + "  1. Скачать одну ссылку: альбом, прямой файл или аккаунт\n"
        + "  2. Скачать все ссылки из links/pending.txt\n"
        + "  3. Проверить аккаунты из links/accs.txt\n"
        + "  4. Найти новые посты аккаунтов и добавить в pending.txt\n"
    )
    print(Fore.WHITE + "Файлы:")
    print(Fore.WHITE + "  links/pending.txt - альбомы, прямые файлы и аккаунты")
    print(Fore.WHITE + "  links/accs.txt   - постоянный список аккаунтов")
    print(
        Fore.WHITE
        + "  Статусы          - ready/failed/banned и ready_accs/failed_accs/banned_accs"
    )

    mode = ask_mode()
    skip_videos = bool(CONFIG["skip_videos"])
    skip_images = bool(CONFIG["skip_images"])
    sort_links = AUTO_SORT_LINKS
    max_connections = DEFAULT_MAX_CONNECTIONS
    parallel_albums = choose_parallel_album_count(99, max_connections)
    log(
        f"{max_connections} соединений, до {parallel_albums} альбомов параллельно, "
        f"сортировка {'включена' if sort_links else 'выключена'}, "
        f"вес альбомов {'включён' if SORT_ALBUMS_BY_SIZE else 'выключен'}",
        Fore.WHITE,
        "CONFIG",
    )

    if mode == "1":
        url = input(Fore.CYAN + "Введите ссылку: " + Style.RESET_ALL).strip()
        result = asyncio.run(dump(url, max_connections, skip_videos, skip_images))
        if result == "success":
            log_done("Готово.")
        elif result == "banned":
            log_done("Ссылка недоступна.", Fore.YELLOW)
        else:
            log_done("Завершено с ошибкой.", Fore.RED)
        return

    if mode == "3":
        asyncio.run(
            batch_download_accounts(
                read_accounts(),
                max_connections,
                skip_videos=False,
                skip_images=False,
                sort_links=sort_links,
            )
        )
        return

    if mode == "4":
        asyncio.run(scan_accounts_to_pending(read_accounts(), sort_links=sort_links))
        return

    asyncio.run(
        batch_download(
            read_pending(),
            max_connections,
            skip_videos,
            skip_images,
            sort_links,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_done("Остановлено пользователем.", Fore.YELLOW)
