import json
from pathlib import Path
from typing import Callable, Iterable

from erome.config import (
    ACCOUNT_STATUS_FILES,
    BASE_DIR,
    CONFIG,
    DOWNLOADS_DIR,
    LINKS_DIR,
    STATUS_FILES,
)
from erome.console import format_bytes, log_warn, now_iso, relative_path
from erome.models import AlbumData, DownloadResult, DownloadResultCallback, DownloadSummary
from erome.urls import (
    _clean_album_title,
    clean_queue_links,
    is_erome_account_url,
    normalize_download_url,
)


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


_manifest_cache: dict | None = None


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
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache

    path = manifest_file_path()
    if not path.exists():
        _manifest_cache = empty_manifest()
        return _manifest_cache
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        bad_path = path.with_suffix(path.suffix + ".bad")
        path.replace(bad_path)
        log_warn(f"Manifest повреждён, старый файл перенесён в {relative_path(bad_path)}")
        loaded = None

    _manifest_cache = _normalize_manifest(loaded)
    return _manifest_cache


def save_manifest(manifest: dict) -> None:
    path = manifest_file_path()
    manifest["updated_at"] = now_iso()
    write_text_atomic(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


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


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


_queue_cache: dict[str, dict[str, None]] = {}


def _queue_entries(name: str) -> dict[str, None]:
    entries = _queue_cache.get(name)
    if entries is None:
        entries = dict.fromkeys(clean_queue_links(read_clean_lines(status_file_path(name))))
        _queue_cache[name] = entries
    return entries


def _flush_queue(name: str) -> None:
    links = list(_queue_entries(name))
    write_text_atomic(
        status_file_path(name),
        "\n".join(links) + ("\n" if links else ""),
    )


def write_queue(name: str, links: Iterable[str]) -> None:
    _queue_cache[name] = dict.fromkeys(clean_queue_links(links))
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


def _get_final_download_path(album_title: str) -> Path:
    final_path = DOWNLOADS_DIR / _clean_album_title(album_title)
    final_path.mkdir(parents=True, exist_ok=True)
    return final_path


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
