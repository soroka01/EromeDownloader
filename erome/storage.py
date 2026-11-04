"""Files under links/ that the console modes work with: the status lists (pending, ready, failed, ...)."""
import os
import time
from pathlib import Path
from typing import Callable, Iterable

from erome.config import ACCOUNT_STATUS_FILES, DOWNLOADS_DIR, LINKS_DIR, STATUS_FILES
from erome.console import log_warn
from erome.models import DownloadResultCallback, Outcome
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


def status_file_path(name: str) -> Path:
    ensure_runtime_dirs()
    return LINKS_DIR / f"{name}.txt"


def read_clean_lines(path: Path) -> list[str]:
    """Non-empty stripped lines of a text file; a BOM is skipped and an unreadable byte never stops the program."""
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if line.strip()
    ]


LOCK_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)  # seconds between the 6 attempts to write or replace a busy file
WINDOWS_BUSY_FILE_ERRORS = (5, 32, 33)  # access denied, sharing violation, lock violation


def _retry_busy_file[T](action: Callable[[], T]) -> T:
    """Run a file operation again when Windows says the file is busy (antivirus, indexer, editor); then re-raise."""
    for delay in LOCK_RETRY_DELAYS:
        try:
            return action()
        except OSError as error:
            busy = isinstance(error, PermissionError) or getattr(error, "winerror", None) in WINDOWS_BUSY_FILE_ERRORS
            if not busy:
                raise
        time.sleep(delay)
    return action()


def write_text_atomic(path: Path, text: str, durable: bool = False) -> None:
    """Write via a temporary file and a rename; `durable` also forces the data to disk before the rename.

    A file that is busy for a moment is retried for about 1.6 seconds in all; the last error is raised after that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")

    def write_temp() -> None:
        with temp_path.open("w", encoding="utf-8") as file:
            file.write(text)
            if durable:
                file.flush()
                os.fsync(file.fileno())

    _retry_busy_file(write_temp)
    _retry_busy_file(lambda: temp_path.replace(path))


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


_failed_flushes: set[str] = set()  # queues whose last write failed: the warning is shown once per failure streak


def _flush_queue(name: str) -> None:
    """Write the queue to its file. Memory stays authoritative: a file that stays busy only costs a warning,
    and the next change writes it again."""
    links = list(_queue_entries(name))
    try:
        write_text_atomic(
            status_file_path(name),
            "\n".join(links) + ("\n" if links else ""),
        )
    except OSError as error:
        if name not in _failed_flushes:
            log_warn(f"links/{name}.txt не записан ({error}); продолжаю, файл обновится при следующем изменении")
        _failed_flushes.add(name)
    else:
        _failed_flushes.discard(name)


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
        self.remaining: dict[str, None] = dict.fromkeys(normalize_download_url(link) for link in clean_queue_links(links))
        # The line each remaining link is written as, worked out once: the file is rewritten after every finished link
        self._lines: dict[str, str | None] = {link: self._file_line(link) for link in self.remaining}

    @staticmethod
    def _file_line(link: str) -> str | None:
        """What write_queue would make of the link; None when it would drop it."""
        cleaned = clean_queue_links([link])
        return canonical_status_link(cleaned[0]) if cleaned else None

    def flush(self) -> None:
        _queue_cache["pending"] = dict.fromkeys(line for line in self._lines.values() if line is not None)
        _flush_queue("pending")

    def remove(self, url: str) -> None:
        normalized = normalize_download_url(url)
        if normalized in self.remaining:
            del self.remaining[normalized]
            del self._lines[normalized]
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
    """Callback that records the outcome of a link in the status files (failed only when it is final)."""

    def mark(url: str, result: Outcome) -> None:
        if result in (Outcome.SUCCESS, Outcome.BANNED):
            set_state(url, "ready" if result == Outcome.SUCCESS else "banned")
        elif failed_is_final:
            set_state(url, "failed")
        else:
            return
        if after:
            after(url)

    return mark
