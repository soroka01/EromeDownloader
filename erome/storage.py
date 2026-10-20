import atexit
import json
import os
import time
from pathlib import Path
from typing import Callable, Iterable

from erome.config import (
    ACCOUNT_STATUS_FILES,
    BASE_DIR,
    DOWNLOADS_DIR,
    LINKS_DIR,
    MANIFEST_PATH,
    STATUS_FILES,
)
from erome.console import format_bytes, log_warn, now_iso, relative_path
from erome.models import AlbumData, DownloadResult, DownloadResultCallback, DownloadSummary
from erome.urls import (
    canonical_status_link,
    clean_queue_links,
    is_erome_account_url,
    normalize_download_url,
    safe_name,
)


def ensure_runtime_dirs() -> None:
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    LINKS_DIR.mkdir(exist_ok=True)


def resolve_base_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def manifest_file_path() -> Path:
    ensure_runtime_dirs()
    path = resolve_base_path(MANIFEST_PATH)
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


_manifest_cache: dict | None = None


_manifest_unreadable = False  # the file on disk could not be read or moved aside: never overwrite it


def _normalize_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        return empty_manifest()
    manifest.setdefault("version", 1)
    manifest.setdefault("created_at", now_iso())
    for section in ("files", "albums", "accounts"):
        if not isinstance(manifest.get(section), dict):
            manifest[section] = {}
    return manifest


def load_manifest() -> dict:
    """Возвращает манифест; файл читается с диска только один раз за запуск."""
    global _manifest_cache, _manifest_unreadable
    if _manifest_cache is not None:
        return _manifest_cache

    try:
        path = manifest_file_path()
        exists = path.exists()
    except OSError as error:  # for example a manifest_path on a drive that does not exist
        _manifest_unreadable = True
        log_warn(f"Путь манифеста недоступен ({error}); отчёт в этом запуске не ведётся")
        _manifest_cache = empty_manifest()
        return _manifest_cache
    if not exists:
        _manifest_cache = empty_manifest()
        return _manifest_cache
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        # The file exists but cannot be read: keep it untouched instead of overwriting the history.
        _manifest_unreadable = True
        log_warn(f"Манифест не читается ({error}); отчёт в этом запуске не обновляется")
        loaded = None
    except ValueError:
        bad_path = path.with_suffix(path.suffix + ".bad")
        try:
            path.replace(bad_path)
            log_warn(f"Manifest повреждён, старый файл перенесён в {relative_path(bad_path)}")
        except OSError as error:
            _manifest_unreadable = True
            log_warn(f"Manifest повреждён и не переносится ({error}); отчёт в этом запуске не обновляется")
        loaded = None

    _manifest_cache = _normalize_manifest(loaded)
    return _manifest_cache


MANIFEST_SAVE_INTERVAL = 5.0  # seconds; rewriting a manifest of thousands of albums takes ~0.1 s


_manifest_dirty = False
_manifest_saved_at = 0.0
_manifest_write_failed = False
_manifest_exit_hook = False


def save_manifest() -> None:
    """Mark the manifest as changed; the file is written at most once per MANIFEST_SAVE_INTERVAL."""
    global _manifest_dirty, _manifest_exit_hook
    _manifest_dirty = True
    if not _manifest_exit_hook:
        atexit.register(flush_manifest)
        _manifest_exit_hook = True
    if time.monotonic() - _manifest_saved_at >= MANIFEST_SAVE_INTERVAL:
        flush_manifest()


def flush_manifest() -> None:
    """Write pending manifest changes. The manifest is only a report, so a write error never stops a download."""
    global _manifest_dirty, _manifest_saved_at, _manifest_write_failed
    manifest = _manifest_cache
    if not _manifest_dirty or manifest is None or _manifest_unreadable:
        return
    _manifest_saved_at = time.monotonic()
    manifest["updated_at"] = now_iso()
    try:
        write_text_atomic(
            manifest_file_path(),
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            durable=True,
        )
    except OSError as error:
        if not _manifest_write_failed:
            log_warn(f"Не удалось записать манифест ({error}); загрузки продолжаются, повторю позже")
        _manifest_write_failed = True
        return
    _manifest_write_failed = False
    _manifest_dirty = False


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
    save_manifest()


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
    save_manifest()


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
    save_manifest()


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


def write_text_atomic(path: Path, text: str, durable: bool = False) -> None:
    """Write via a temporary file and a rename; `durable` also forces the data to disk before the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        file.write(text)
        if durable:
            file.flush()
            os.fsync(file.fileno())
    temp_path.replace(path)


_queue_cache: dict[str, dict[str, None]] = {}


def reset_queue_cache() -> None:
    """Forget the in-memory copy of links/*.txt, so a file edited by hand is read again."""
    _queue_cache.clear()


def _entries_from(links: Iterable[str]) -> dict[str, None]:
    return dict.fromkeys(canonical_status_link(link) for link in clean_queue_links(links))


def _queue_entries(name: str) -> dict[str, None]:
    entries = _queue_cache.get(name)
    if entries is None:
        entries = _entries_from(read_clean_lines(status_file_path(name)))
        _queue_cache[name] = entries
    return entries


def _flush_queue(name: str) -> None:
    links = list(_queue_entries(name))
    write_text_atomic(
        status_file_path(name),
        "\n".join(links) + ("\n" if links else ""),
    )


def write_queue(name: str, links: Iterable[str]) -> None:
    _queue_cache[name] = _entries_from(links)
    _flush_queue(name)


def read_queue(name: str) -> list[str]:
    path = status_file_path(name)
    if not path.exists():
        path.touch()
        return []
    return list(_queue_entries(name))


def add_status(url: str, status: str) -> None:
    entries = _queue_entries(status)
    if url not in entries:
        entries[url] = None
        _flush_queue(status)


def remove_status(url: str, status: str) -> None:
    entries = _queue_entries(status)
    if url in entries:
        del entries[url]
        _flush_queue(status)


def _set_exclusive_status(url: str, status_file: str, group: Iterable[str]) -> None:
    for name in group:
        if name != status_file:
            remove_status(url, name)
    add_status(url, status_file)


def set_status(url: str, status: str) -> None:
    _set_exclusive_status(url, status, STATUS_FILES)


def set_account_status(url: str, status: str) -> None:
    _set_exclusive_status(url, f"{status}_accs", ACCOUNT_STATUS_FILES)


def set_download_status(url: str, status: str) -> None:
    if is_erome_account_url(url):
        set_account_status(url, status)
    else:
        set_status(url, status)


def read_status_set(status: str) -> set[str]:
    return set(_queue_entries(status))


def write_pending(links: Iterable[str]) -> None:
    write_queue("pending", links)


def read_pending() -> list[str]:
    return read_queue("pending")


def append_pending_links(links: Iterable[str]) -> int:
    entries = _queue_entries("pending")
    additions = 0
    for link in links:
        normalized = normalize_download_url(link)
        if normalized and normalized not in entries:
            entries[normalized] = None
            additions += 1
    if additions:
        _flush_queue("pending")
    return additions


class PendingQueue:
    def __init__(self, links: Iterable[str]):
        self.remaining: dict[str, None] = dict.fromkeys(
            normalize_download_url(link)
            for link in links
            if link and link.strip()
        )

    def flush(self) -> None:
        write_pending(self.remaining)

    def remove(self, url: str) -> None:
        normalized = normalize_download_url(url)
        if normalized in self.remaining:
            del self.remaining[normalized]
            self.flush()


def read_accounts() -> list[str]:
    return read_queue("accs")


def album_download_dir(album_title: str) -> Path:
    folder = DOWNLOADS_DIR / safe_name(album_title)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def result_marker(
    set_state: Callable[[str, str], None],
    failed_is_final: bool,
    after: Callable[[str], None] | None = None,
) -> DownloadResultCallback:
    """Колбэк, фиксирующий итог ссылки в status-файлах (failed — только если он финальный)."""

    def mark(url: str, result: str) -> None:
        if result in {"success", "banned"}:
            set_state(url, "ready" if result == "success" else "banned")
        elif failed_is_final:
            set_state(url, "failed")
        else:
            return
        if after:
            after(url)

    return mark
