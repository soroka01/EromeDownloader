"""Persistent state of the Telegram mode: the job queue and the ledger of what was already delivered.

The queue (`links/tg_queue.json`) is rewritten atomically on every structural change, so a link the bot
acknowledged survives a crash. The ledger (`links/tg_sent.jsonl`) is append-only: one line per delivered batch
(`k`), per album that is complete (`done`), per set of files Telegram refused for good (`x`) and per `/resend`
(`reset`). It is what keeps a restart or a repeated link from sending anything twice or downloading a file that
Telegram will refuse again.
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
from urllib.parse import urlparse

from erome.config import LINKS_DIR
from erome.console import log_warn
from erome.models import LinkKind
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
    kind: LinkKind
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
    tries: int = 0  # times the page could not be read for a temporary reason (the job waited and started again)
    not_before: float = 0.0  # epoch seconds; a job that waits for another try is not started earlier

    def __post_init__(self) -> None:
        self.kind = LinkKind(self.kind)  # the queue file stores the plain string

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


class TelegramState:
    def __init__(self, state_path: Path = STATE_PATH, ledger_path: Path = LEDGER_PATH) -> None:
        self.state_path = state_path
        self.ledger_path = ledger_path
        self.queue: list[Job] = []
        self.history: list[Job] = []
        self.panels: dict[str, int] = {}
        self._sent: dict[str, set[str]] = {}
        self._done: set[str] = set()
        self._refused: dict[str, dict[str, tuple[int, str]]] = {}  # album -> {file key: (upload limit then, reason)}

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
                log_warn("В очереди Telegram есть запись, которую не удалось прочитать; она пропущена")
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
        url = normalize_download_url(url)
        return next((job for job in self.queue if job.url == url), None)

    def next_queued(self) -> Job | None:
        """The job to start next. A profile goes first: reading it only adds albums (right behind it, so their order in
        the queue stays that of the links), and the owner learns at once whether the profile can be read at all."""
        now = time.time()
        ready = [job for job in self.queue if job.status == QUEUED and job.not_before <= now]
        return next((job for job in ready if job.kind == LinkKind.ACCOUNT), ready[0] if ready else None)

    def jobs_before(self, job: Job) -> list[Job]:
        """The jobs in front of `job` in the queue, the one being processed included."""
        return self.queue[: self.queue.index(job)] if job in self.queue else list(self.queue)

    def queued(self) -> list[Job]:
        return [job for job in self.queue if job.status == QUEUED]

    def running(self) -> Job | None:
        return next((job for job in self.queue if job.status == RUNNING), None)

    def _new_job(self, kind: LinkKind, url: str, parent: str = "") -> Job:
        return Job(id=uuid.uuid4().hex[:10], kind=kind, url=url, added=time.time(), parent=parent)

    def add(self, kind: LinkKind, url: str) -> Job | None:
        """Append a job; None when the same link is already queued or running."""
        url = normalize_download_url(url)
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

    def defer(self, job: Job, seconds: float, note: str) -> None:
        """A temporary problem (the site does not answer): the job goes back and is not started for `seconds`."""
        if job in self.queue:
            job.status = QUEUED
            job.tries += 1
            job.not_before = time.time() + seconds
            job.note = note
            self.save()

    def finish(self, job: Job, status: str, note: str = "") -> None:
        if any(done.id == job.id for done in self.history):
            return  # already finished (for example a profile job replaced by its albums)
        self._archive(job, status, note)
        self.save()

    def _archive(self, job: Job, status: str, note: str) -> None:
        """Move a job from the queue to the history (the caller saves)."""
        job.status = status
        job.note = note
        job.finished = time.time()
        if job in self.queue:
            self.queue.remove(job)
        self.history.append(job)
        del self.history[:-HISTORY_LIMIT]

    def expand_account(self, job: Job, album_urls: Iterable[str], note: str) -> int:
        """Replace a finished profile scan by its albums (one atomic write, albums go next in the queue)."""
        index = self.queue.index(job) + 1 if job in self.queue else len(self.queue)
        added: list[Job] = []
        known = {item.url for item in self.queue}
        for url in album_urls:
            url = normalize_download_url(url)
            if url and url not in known:
                known.add(url)
                added.append(self._new_job(LinkKind.ALBUM, url, parent=job.id))
        self.queue[index:index] = added
        self._archive(job, DONE, note)
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
        self._refused.clear()
        if not self.ledger_path.exists():
            return
        with self.ledger_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    album = str(record["a"])
                    self._replay(album, record)
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue  # a line cut short by a crash

    def _replay(self, album: str, record: dict[str, Any]) -> None:
        if record.get("reset"):
            self._sent.pop(album, None)
            self._done.discard(album)
            self._refused.pop(album, None)
        elif record.get("done"):
            self._done.add(album)
        elif isinstance(record.get("x"), dict):  # files Telegram refused for good
            limit = int(record.get("lim") or 0)
            entries = self._refused.setdefault(album, {})
            for key, reason in record["x"].items():
                entries[str(key)] = (limit, str(reason))
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
        return set(self._sent.get(normalize_download_url(album_url), ()))

    def is_album_done(self, album_url: str) -> bool:
        return normalize_download_url(album_url) in self._done

    def record_batch(self, album_url: str, keys: Iterable[str], nbytes: int) -> None:
        album_url = normalize_download_url(album_url)
        keys = list(keys)
        if not keys:
            return
        self._append({"a": album_url, "k": keys, "b": int(nbytes)})
        self._sent.setdefault(album_url, set()).update(keys)

    def refused(self, album_url: str, limit: int) -> dict[str, str]:
        """{file key: reason} of the files Telegram refused for good, valid for an upload limit of `limit` bytes.

        A refusal recorded under a smaller limit is not valid any more (the account may have got Premium since).
        """
        album_url = normalize_download_url(album_url)
        sent = self._sent.get(album_url, ())
        return {
            key: reason
            for key, (then, reason) in self._refused.get(album_url, {}).items()
            if (then == 0 or then >= limit) and key not in sent
        }

    def record_refused(self, album_url: str, reasons: dict[str, str], limit: int) -> None:
        album_url = normalize_download_url(album_url)
        reasons = {key: reason[:120] for key, reason in reasons.items()}
        if not reasons:
            return
        self._append({"a": album_url, "x": reasons, "lim": int(limit)})
        entries = self._refused.setdefault(album_url, {})
        for key, reason in reasons.items():
            entries[key] = (int(limit), reason)

    def mark_album_done(self, album_url: str, title: str = "") -> None:
        album_url = normalize_download_url(album_url)
        if album_url not in self._done:
            self._append({"a": album_url, "done": 1, "n": title[:120]})
            self._done.add(album_url)

    def reset_album(self, album_url: str) -> None:
        album_url = normalize_download_url(album_url)
        if album_url in self._sent or album_url in self._done or album_url in self._refused:
            self._append({"a": album_url, "reset": 1})
            self._sent.pop(album_url, None)
            self._done.discard(album_url)
            self._refused.pop(album_url, None)
