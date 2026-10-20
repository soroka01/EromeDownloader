"""links/manifest.json: a report of the files, albums and accounts that were processed. Nothing is decided from it.

It is loaded once, changed in memory and written in batches (rewriting a manifest of thousands of albums takes about
a tenth of a second), so a slow or failing disk can neither hold back nor fail a download.
"""
import atexit
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from erome.config import BASE_DIR, MANIFEST_PATH
from erome.console import log_warn
from erome.models import AlbumData, DownloadResult, DownloadSummary, Outcome
from erome.storage import ensure_runtime_dirs, write_text_atomic


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_bytes(size: int | float) -> str:
    """Size as the manifest writes it ("12.34 MB"). The strings are part of the file format: do not swap in ui.human_size."""
    size = float(size or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def relative_path(path: Path | None) -> str:
    """Path as the manifest records it: relative to the project when possible."""
    if not path:
        return ""
    try:
        return str(path.resolve().relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def manifest_file_path() -> Path:
    ensure_runtime_dirs()
    path = Path(MANIFEST_PATH)
    path = path if path.is_absolute() else BASE_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _empty_manifest() -> dict:
    return {
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "files": {},
        "albums": {},
        "accounts": {},
    }


def _normalize(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        return _empty_manifest()
    manifest.setdefault("version", 1)
    manifest.setdefault("created_at", now_iso())
    for section in ("files", "albums", "accounts"):
        if not isinstance(manifest.get(section), dict):
            manifest[section] = {}
    return manifest


class ManifestStore:
    SAVE_INTERVAL = 5.0  # seconds between writes while records keep coming

    def __init__(self) -> None:
        self._data: dict | None = None
        self._unreadable = False  # the file on disk could not be read or moved aside: it is never overwritten
        self._dirty = False
        self._saved_at = 0.0
        self._write_failed = False

    @property
    def data(self) -> dict:
        """The manifest; the file is read from disk only once per run."""
        if self._data is None:
            self._data = self._load()
        return self._data

    def _load(self) -> dict:
        try:
            path = manifest_file_path()
            exists = path.exists()
        except OSError as error:  # for example a manifest_path on a drive that does not exist
            self._unreadable = True
            log_warn(f"Путь манифеста недоступен ({error}); отчёт в этом запуске не ведётся")
            return _empty_manifest()
        if not exists:
            return _empty_manifest()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            # The file exists but cannot be read: keep it untouched instead of overwriting the history.
            self._unreadable = True
            log_warn(f"Манифест не читается ({error}); отчёт в этом запуске не обновляется")
            loaded = None
        except ValueError:
            bad_path = path.with_suffix(path.suffix + ".bad")
            try:
                path.replace(bad_path)
                log_warn(f"Manifest повреждён, старый файл перенесён в {relative_path(bad_path)}")
            except OSError as error:
                self._unreadable = True
                log_warn(f"Manifest повреждён и не переносится ({error}); отчёт в этом запуске не обновляется")
            loaded = None
        return _normalize(loaded)

    def mark_changed(self) -> None:
        """Note that the manifest changed; the file is written at most once per SAVE_INTERVAL."""
        self._dirty = True
        if time.monotonic() - self._saved_at >= self.SAVE_INTERVAL:
            self.flush()

    def flush(self) -> None:
        """Write pending changes. The manifest is only a report, so a write error never stops a download."""
        if not self._dirty or self._data is None or self._unreadable:
            return
        self._saved_at = time.monotonic()
        self._data["updated_at"] = now_iso()
        try:
            write_text_atomic(
                manifest_file_path(),
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                durable=True,
            )
        except OSError as error:
            if not self._write_failed:
                log_warn(f"Не удалось записать манифест ({error}); загрузки продолжаются, повторю позже")
            self._write_failed = True
            return
        self._write_failed = False
        self._dirty = False


MANIFEST = ManifestStore()


def flush_manifest() -> None:
    """Write what is pending: at the end of every menu action and when the program exits."""
    MANIFEST.flush()


atexit.register(flush_manifest)


def record_file_results(
    referrer_url: str,
    download_path: Path,
    results: Iterable[DownloadResult],
) -> None:
    files = MANIFEST.data["files"]
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
    MANIFEST.mark_changed()


def record_album_manifest(
    album_data: AlbumData,
    download_path: Path,
    summary: DownloadSummary,
    status: Outcome,
) -> None:
    MANIFEST.data["albums"][album_data.url] = {
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
    MANIFEST.mark_changed()


def record_account_manifest(
    account_url: str,
    status: str,
    found: int = 0,
    new: int = 0,
    added_to_pending: int = 0,
) -> None:
    """Accounts are recorded with the status-file vocabulary: "ready", "failed" or "banned"."""
    MANIFEST.data["accounts"][account_url] = {
        "url": account_url,
        "status": status,
        "albums_found": found,
        "albums_new": new,
        "added_to_pending": added_to_pending,
        "updated_at": now_iso(),
    }
    MANIFEST.mark_changed()
