"""Persistent state of the Telegram mode: the job queue and the ledger of what was already delivered.

The queue (`links/tg_queue.json`) is rewritten atomically on every structural change, so a link the bot
acknowledged survives a crash. The ledger (`links/tg_sent.jsonl`) is append-only: one line per delivered batch,
which is what keeps a restart or a repeated link from sending anything twice.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

from erome.config import LINKS_DIR
from erome.console import log_warn
from erome.storage import ensure_runtime_dirs, write_text_atomic
from erome.urls import normalize_download_url

STATE_PATH = LINKS_DIR / "tg_queue.json"
LEDGER_PATH = LINKS_DIR / "tg_sent.jsonl"
HISTORY_LIMIT = 50

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
PARTIAL = "partial"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = frozenset({DONE, PARTIAL, FAILED, CANCELLED})


def canonical_url(url: str) -> str:
    """An album/profile link in the one spelling used as a key: the site answers on erome.com and www.erome.com."""
    url = normalize_download_url(url)
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() in ("erome.com", "www.erome.com") and parsed.path:
        return urlunparse(("https", "www.erome.com", parsed.path.rstrip("/") or "/", "", "", ""))
    return url


def file_key(url: str) -> str:
    """Stable id of a media file: the URL path only (CDN shard and `?v=` stamp change between page loads)."""
    return hashlib.sha1(urlparse(url).path.encode("utf-8")).hexdigest()[:12]


def random_id_for(job_id: str, key: str) -> int:
    """Deterministic Telegram random_id: repeating a send of the same file in the same job is deduplicated."""
    digest = hashlib.sha256(f"{job_id}|{key}".encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True) or 1


@dataclass
class Job:
    id: str
    kind: str  # "album" | "account"
    url: str
    status: str = QUEUED
    added: float = 0.0
    finished: float = 0.0
    title: str = ""
    parent: str = ""
    files_total: int = 0
    files_sent: int = 0
    bytes_sent: int = 0
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def is_open(self) -> bool:
        return self.status in (QUEUED, RUNNING)


class TelegramState:
    def __init__(self, state_path: Path = STATE_PATH, ledger_path: Path = LEDGER_PATH) -> None:
        self.state_path = state_path
        self.ledger_path = ledger_path
        self.queue: list[Job] = []
        self.history: list[Job] = []
        self.panels: dict[str, int] = {}
        self._sent: dict[str, set[str]] = {}
        self._done: set[str] = set()

    # ----- persistence ------------------------------------------------------------------------

    def load(self) -> None:
        ensure_runtime_dirs()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_queue()
        self._load_ledger()

    def _load_queue(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("не объект JSON")
        except (OSError, ValueError) as exc:
            bad = self.state_path.with_suffix(self.state_path.suffix + ".bad")
            try:
                self.state_path.replace(bad)
            except OSError:
                pass
            log_warn(f"Очередь Telegram повреждена ({exc}); старый файл сохранён как {bad.name}")
            return
        self.queue = self._jobs(data.get("queue"))
        self.history = self._jobs(data.get("history"))[-HISTORY_LIMIT:]
        for job in self.queue:
            if job.status != QUEUED:  # interrupted mid-run: starts over, the ledger skips what was delivered
                job.status = QUEUED
        panels = data.get("panels")
        if isinstance(panels, dict):
            for chat_id, message_id in panels.items():
                try:
                    self.panels[str(chat_id)] = int(message_id)
                except (TypeError, ValueError):
                    continue

    @staticmethod
    def _jobs(items: Any) -> list[Job]:
        jobs: list[Job] = []
        for item in items if isinstance(items, list) else []:
            try:
                jobs.append(Job.from_dict(item))
            except (TypeError, ValueError):
                continue
        return jobs

    def save(self) -> None:
        payload = {
            "version": 1,
            "queue": [asdict(job) for job in self.queue],
            "history": [asdict(job) for job in self.history[-HISTORY_LIMIT:]],
            "panels": self.panels,
        }
        write_text_atomic(self.state_path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")

    # ----- queue ------------------------------------------------------------------------------

    def find_open(self, url: str) -> Job | None:
        url = canonical_url(url)
        return next((job for job in self.queue if job.url == url), None)

    def next_queued(self) -> Job | None:
        return next((job for job in self.queue if job.status == QUEUED), None)

    def queued(self) -> list[Job]:
        return [job for job in self.queue if job.status == QUEUED]

    def running(self) -> Job | None:
        return next((job for job in self.queue if job.status == RUNNING), None)

    def _new_job(self, kind: str, url: str, parent: str = "") -> Job:
        return Job(id=uuid.uuid4().hex[:10], kind=kind, url=url, added=time.time(), parent=parent)

    def add(self, kind: str, url: str) -> Job | None:
        """Append a job; None when the same link is already queued or running."""
        url = canonical_url(url)
        if not url or self.find_open(url):
            return None
        job = self._new_job(kind, url)
        self.queue.append(job)
        self.save()
        return job

    def mark_running(self, job: Job) -> None:
        job.status = RUNNING
        self.save()

    def requeue(self, job: Job) -> None:
        if job in self.queue:
            job.status = QUEUED
            self.save()

    def finish(self, job: Job, status: str, note: str = "") -> None:
        if any(done.id == job.id for done in self.history):
            return  # already finished (for example a profile job replaced by its albums)
        job.status = status
        job.note = note
        job.finished = time.time()
        if job in self.queue:
            self.queue.remove(job)
        self.history.append(job)
        del self.history[:-HISTORY_LIMIT]
        self.save()

    def expand_account(self, job: Job, album_urls: Iterable[str], note: str) -> int:
        """Replace a finished profile scan by its albums (one atomic write, albums go next in the queue)."""
        index = self.queue.index(job) + 1 if job in self.queue else len(self.queue)
        added: list[Job] = []
        known = {item.url for item in self.queue}
        for url in album_urls:
            url = canonical_url(url)
            if url and url not in known:
                known.add(url)
                added.append(self._new_job("album", url, parent=job.id))
        self.queue[index:index] = added
        job.status = DONE
        job.note = note
        job.finished = time.time()
        if job in self.queue:
            self.queue.remove(job)
        self.history.append(job)
        del self.history[:-HISTORY_LIMIT]
        self.save()
        return len(added)

    def clear_queued(self) -> int:
        removed = [job for job in self.queue if job.status == QUEUED]
        if removed:
            self.queue = [job for job in self.queue if job.status != QUEUED]
            self.save()
        return len(removed)

    # ----- panels -----------------------------------------------------------------------------

    def panel_id(self, chat_id: int) -> int | None:
        return self.panels.get(str(chat_id))

    def set_panel(self, chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            self.panels.pop(str(chat_id), None)
        else:
            self.panels[str(chat_id)] = int(message_id)
        self.save()

    # ----- ledger -----------------------------------------------------------------------------

    def _load_ledger(self) -> None:
        self._sent.clear()
        self._done.clear()
        if not self.ledger_path.exists():
            return
        with self.ledger_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    album = str(record["a"])
                except (ValueError, KeyError, TypeError):
                    continue  # a line cut short by a crash
                if record.get("reset"):
                    self._sent.pop(album, None)
                    self._done.discard(album)
                elif record.get("done"):
                    self._done.add(album)
                elif isinstance(record.get("k"), list):
                    self._sent.setdefault(album, set()).update(str(key) for key in record["k"])

    def _append(self, record: dict[str, Any]) -> None:
        record["t"] = int(time.time())
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def sent_keys(self, album_url: str) -> set[str]:
        return set(self._sent.get(canonical_url(album_url), ()))

    def is_album_done(self, album_url: str) -> bool:
        return canonical_url(album_url) in self._done

    def record_batch(self, album_url: str, keys: Iterable[str], nbytes: int) -> None:
        album_url = canonical_url(album_url)
        keys = list(keys)
        if not keys:
            return
        self._append({"a": album_url, "k": keys, "b": int(nbytes)})
        self._sent.setdefault(album_url, set()).update(keys)

    def mark_album_done(self, album_url: str, title: str = "") -> None:
        album_url = canonical_url(album_url)
        if album_url not in self._done:
            self._append({"a": album_url, "done": 1, "n": title[:120]})
            self._done.add(album_url)

    def reset_album(self, album_url: str) -> None:
        album_url = canonical_url(album_url)
        if album_url in self._sent or album_url in self._done:
            self._append({"a": album_url, "reset": 1})
            self._sent.pop(album_url, None)
            self._done.discard(album_url)
