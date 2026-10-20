"""The job worker of the Telegram mode: read a link, download its files, post them, remember what was posted.

Jobs run one after another. Inside an album the files are cut into batches of up to 10; the next batch is
downloaded while the current one is being uploaded, and every batch that Telegram accepted is written to the
ledger before its local files are deleted - so a crash or a restart never loses a file and never posts one twice.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
from pathlib import Path

from erome.config import CONFIG, DEFAULT_MAX_CONNECTIONS, DOWNLOADS_DIR, UNAVAILABLE_STATUSES
from erome.console import (
    DownloadSpeed,
    checkpoint,
    friendly_error,
    log,
    log_error,
    log_success,
    log_warn,
    short_url,
    shorten_text,
    stop_requested,
)
from erome.downloader import download_jobs
from erome.models import AlbumData, AlbumFetchError, DownloadResult, FileJob
from erome.net import _collect_album_data, collect_account_album_urls
from erome.tg_media import SendItem, prepare_item
from erome.tg_settings import TelegramSettings
from erome.tg_state import (
    CANCELLED,
    DONE,
    FAILED,
    PARTIAL,
    Job,
    TelegramState,
    file_key,
    random_id_for,
)
from erome.tg_userbot import (
    ALBUM_MAX_ITEMS,
    TRANSIENT_ERRORS,
    DestinationError,
    RetryLater,
    Userbot,
    describe_error,
    task_is_cancelling,
)
from erome.ui import human_size, plural
from erome.urls import _clean_album_title, account_name_from_url, build_download_jobs

LOG = logging.getLogger("erome.tg")

STAGING_DIR = DOWNLOADS_DIR / "_telegram"
BATCH_SIZE = ALBUM_MAX_ITEMS
MAX_SEND_ATTEMPTS = 3
TRANSIENT_PAUSE_SECONDS = 10
MAX_TRANSIENT_STREAK = 30  # failures with a live connection tolerated in a row (5 minutes)
DESTINATION_RETRY_SECONDS = 60
SCAN_ATTEMPTS = 3
_STAGING_CODE_RE = re.compile(r"\[([^\[\]]+)\]$")


class JobFailed(Exception):
    """The job cannot continue; the message is shown to the user."""


class StopRequested(Exception):
    """The program is closing: the job goes back to the queue and continues on the next start."""


class Progress:
    """Live numbers of the job being processed (read by the bot panel and the console line)."""

    def __init__(self, job: Job) -> None:
        self.job = job
        self.title = job.title
        self.stage = "читаю страницу"
        self.started = time.monotonic()
        self.files_total = 0
        self.files_downloaded = 0
        self.files_sent = 0
        self.bytes_downloaded = 0
        self.bytes_uploaded = 0
        self.bytes_delivered = 0
        self.download_speed = DownloadSpeed()
        self.upload_speed = DownloadSpeed()
        self.staging: Path | None = None

    def add_download(self, count: int) -> None:
        self.bytes_downloaded += count
        self.download_speed.add(count)

    def add_upload(self, count: int) -> None:
        self.bytes_uploaded += count
        self.upload_speed.add(count)

    def eta(self) -> float | None:
        """Rough time to the end of the album from the pace of delivered files."""
        elapsed = time.monotonic() - self.started
        if self.files_sent < 3 or elapsed < 10 or self.files_total <= self.files_sent:
            return None
        return elapsed / self.files_sent * (self.files_total - self.files_sent)


def code_of(album_url: str) -> str:
    return album_url.rstrip("/").rsplit("/", 1)[-1]


class Pipeline:
    def __init__(
        self,
        state: TelegramState,
        userbot: Userbot,
        settings: TelegramSettings,
        staging: Path = STAGING_DIR,
    ) -> None:
        self.state = state
        self.userbot = userbot
        self.settings = settings
        self.staging = staging
        self.progress: Progress | None = None
        self.waiting = ""  # why nothing moves right now (no connection, flood wait, ...)
        self.blocked = ""  # the target chat refuses our messages
        self._wake = asyncio.Event()
        self._job_task: asyncio.Task[tuple[str, str] | None] | None = None
        self._cancel_job_id = ""
        self._stopping = False
        self._failures: dict[str, str] = {}  # file url -> why it was not delivered (current job)
        self._gone: set[str] = set()  # files the site reports as removed: not worth another try
        self._last_blocked = ""

    # ----- control (called by the bot) ----------------------------------------------------------

    def submit(self, kind: str, url: str) -> Job | None:
        job = self.state.add(kind, url)
        if job is not None:
            self._wake.set()
        return job

    def cancel_current(self) -> bool:
        job = self.state.running()
        task = self._job_task
        if job is None or task is None or task.done():
            return False
        self._cancel_job_id = job.id
        task.cancel()
        return True

    def clear_queue(self) -> int:
        return self.state.clear_queued()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._job_task is not None and not self._job_task.done():
            self._job_task.cancel()

    # ----- worker loop ---------------------------------------------------------------------------

    async def run(self) -> None:
        self._sweep_staging()
        while not self._stopping:
            if await checkpoint():  # waits while paused; True = the user asked to stop
                break
            job = self.state.next_queued()
            if job is None:
                self._wake.clear()
                # Woken by a new link; the timeout lets an idle worker notice the Q hot key too.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 1.0)
                continue
            await self._process(job)

    async def _process(self, job: Job) -> None:
        self.state.mark_running(job)
        self.progress = Progress(job)
        self._failures = {}
        self._gone = set()
        task = asyncio.ensure_future(self._run_job(job))
        self._job_task = task
        finished = True
        try:
            outcome = await task
        except asyncio.CancelledError:
            if task_is_cancelling():  # the worker itself is being stopped
                finished = False
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                self.state.requeue(job)
                raise
            if self._cancel_job_id == job.id:
                self._finish(job, CANCELLED, "отменено")
            else:  # stop() cancelled it: the job continues on the next start
                self.state.requeue(job)
                finished = False
        except StopRequested:
            self.state.requeue(job)
            finished = False
        except JobFailed as exc:
            self._finish(job, FAILED, str(exc))
        except Exception as exc:
            LOG.exception("Job %s crashed", job.url)
            self._finish(job, FAILED, f"внутренняя ошибка: {type(exc).__name__}: {exc}"[:200])
        else:
            if outcome is not None:  # None: the job finalized itself (profile expansion)
                self._finish(job, *outcome)
        finally:
            staging = self.progress.staging if self.progress else None
            self._job_task = None
            self._cancel_job_id = ""
            self.progress = None
            self.waiting = ""
            if finished and staging is not None and self.settings.delete_after_upload:
                shutil.rmtree(staging, ignore_errors=True)

    def _finish(self, job: Job, status: str, note: str) -> None:
        self.state.finish(job, status, note)
        label = shorten_text(job.title or short_url(job.url), 70)
        if status == DONE:
            log_success(f"«{label}»: {note}")
        elif status == PARTIAL:
            log_warn(f"«{label}»: {note}")
        elif status == CANCELLED:
            log_warn(f"«{label}»: отменено")
        else:
            log_error(f"«{label}»: {note}")

    async def _run_job(self, job: Job) -> tuple[str, str] | None:
        if job.kind == "account":
            return await self._run_account(job)
        return await self._run_album(job)

    # ----- profile ---------------------------------------------------------------------------------

    async def _run_account(self, job: Job) -> tuple[str, str] | None:
        assert self.progress is not None
        name = account_name_from_url(job.url)
        job.title = self.progress.title = f"Профиль {name}"
        self.progress.stage = "читаю профиль"
        log(f"Профиль {name}: читаю список альбомов")
        try:
            album_urls = await collect_account_album_urls(job.url, show_progress=False)
        except AlbumFetchError as error:
            if error.status in UNAVAILABLE_STATUSES:
                raise JobFailed("профиль недоступен (удалён или закрыт)") from None
            raise JobFailed(f"не удалось открыть профиль: {friendly_error(error)}") from None
        if not album_urls:
            raise JobFailed("в профиле не найдено альбомов")
        # The site lists the newest first; the group reads like a chat, so the oldest album goes first.
        total = len(album_urls)
        fresh = [url for url in reversed(album_urls) if not self.state.is_album_done(url)]
        fresh = [url for url in fresh if self.state.find_open(url) is None]
        if fresh:
            note = f"в очередь добавлено {len(fresh)} из {total}"
            if len(fresh) < total:
                note += " (остальные уже отправлены или в очереди)"
        else:
            note = f"новых альбомов нет: все {total} уже отправлены или в очереди"
        self.state.expand_account(job, fresh, note)  # one atomic write: the albums replace the profile job
        log_success(f"Профиль {name}: {note}")
        return None

    # ----- album -----------------------------------------------------------------------------------

    def _staging_dir(self, album: AlbumData) -> Path:
        title = _clean_album_title(album.title)[:80].strip(". ") or "album"
        return self.staging / f"{title} [{code_of(album.url)}]"

    async def _scan_album(self, job: Job) -> AlbumData:
        last_error = ""
        for attempt in range(1, SCAN_ATTEMPTS + 1):
            try:
                return await _collect_album_data(
                    job.url, bool(CONFIG["skip_videos"]), bool(CONFIG["skip_images"])
                )
            except AlbumFetchError as error:
                if error.status in UNAVAILABLE_STATUSES:
                    raise JobFailed("альбом недоступен (удалён или закрыт)") from None
                last_error = friendly_error(error)
            if attempt < SCAN_ATTEMPTS:
                await self._sleep(5 * attempt)
        raise JobFailed(f"страница альбома не открывается: {last_error}")

    async def _run_album(self, job: Job) -> tuple[str, str] | None:
        progress = self.progress
        assert progress is not None
        album = await self._scan_album(job)
        job.title = progress.title = album.title
        if not album.urls:
            raise JobFailed("в альбоме нет файлов")

        staging = self._staging_dir(album)
        progress.staging = staging
        sent_keys = self.state.sent_keys(album.url)
        # Names are fixed for the whole album, so batches never collide in the staging folder.
        todo = [item for item in build_download_jobs(album.urls, staging) if file_key(item.url) not in sent_keys]
        job.files_total = progress.files_total = len(todo)
        if not todo:
            self.state.mark_album_done(album.url, album.title)
            return DONE, "всё уже было отправлено"
        log(
            f"«{shorten_text(album.title, 60)}»: к отправке {len(todo)} из {album.media_count} "
            f"{plural(album.media_count, 'файла', 'файлов', 'файлов')}"
        )

        caption = f"{album.title}\n{album.url}"[:900]
        caption_pending = True
        batches = [todo[i : i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
        fetches: dict[int, asyncio.Task[dict[str, DownloadResult]]] = {}
        failed: list[FileJob] = []

        def start_fetch(index: int) -> asyncio.Task[dict[str, DownloadResult]]:
            if index not in fetches:
                fetches[index] = asyncio.ensure_future(self._fetch(album, batches[index], staging))
            return fetches[index]

        try:
            for index, batch in enumerate(batches):
                if await checkpoint():
                    raise StopRequested
                progress.stage = "скачиваю"
                results = await start_fetch(index)
                fetches.pop(index, None)
                if index + 1 < len(batches):
                    start_fetch(index + 1)  # downloads while this batch is uploaded
                delivered = await self._handle_batch(
                    job, album, batch, results, caption if caption_pending else "", failed
                )
                caption_pending = caption_pending and not delivered
            # One more round for files that failed to download (not for those the site reports as gone).
            retry = [item for item in failed if item.url not in self._gone]
            if retry:
                progress.stage = "повторяю не скачавшееся"
                failed = [item for item in failed if item.url in self._gone]
                await self._sleep(3)
                for start in range(0, len(retry), BATCH_SIZE):
                    chunk = retry[start : start + BATCH_SIZE]
                    if await checkpoint():
                        raise StopRequested
                    results = await self._fetch(album, chunk, staging)
                    delivered = await self._handle_batch(
                        job, album, chunk, results, caption if caption_pending else "", failed
                    )
                    caption_pending = caption_pending and not delivered
        finally:
            for task in fetches.values():
                task.cancel()
            await asyncio.gather(*fetches.values(), return_exceptions=True)

        sent = progress.files_sent
        if sent >= len(todo):
            self.state.mark_album_done(album.url, album.title)
            return DONE, f"{sent} {plural(sent, 'файл', 'файла', 'файлов')}, {human_size(job.bytes_sent)}"
        delivered_keys = self.state.sent_keys(album.url)
        left = [item for item in todo if file_key(item.url) not in delivered_keys]
        if left and all(item.url in self._gone for item in left):
            # Only files the site has removed are missing: more attempts are pointless, profile scans may skip it.
            self.state.mark_album_done(album.url, album.title)
        missing = len(todo) - sent
        reasons = sorted({reason for reason in self._failures.values() if reason})
        detail = f" ({'; '.join(reasons[:2])})" if reasons else ""
        note = f"отправлено {sent} из {len(todo)}, не удалось {missing}{detail}"
        return (PARTIAL if sent else FAILED), note

    async def _fetch(self, album: AlbumData, batch: list[FileJob], staging: Path) -> dict[str, DownloadResult]:
        progress = self.progress
        assert progress is not None
        results: dict[str, DownloadResult] = {}

        def on_file(result: DownloadResult) -> None:
            results[result.url] = result
            if result.ok:
                progress.files_downloaded += 1

        await download_jobs(
            album.url,
            batch,
            DEFAULT_MAX_CONNECTIONS,
            staging,
            show_progress=False,
            show_summary=False,
            byte_progress=progress.add_download,
            file_result_callback=on_file,
            record=False,
        )
        return results

    async def _handle_batch(
        self,
        job: Job,
        album: AlbumData,
        batch: list[FileJob],
        results: dict[str, DownloadResult],
        caption: str,
        failed: list[FileJob],
    ) -> bool:
        """Post the downloaded files of a batch. Returns True when at least one file was delivered."""
        good: list[FileJob] = []
        for item in batch:
            result = results.get(item.url)
            if result is not None and result.ok and result.path is not None and result.path.exists():
                good.append(item)
                self._failures.pop(item.url, None)
                continue
            failed.append(item)
            if result is not None and result.status == "banned":
                self._gone.add(item.url)
                reason = f"файл недоступен на Erome ({result.error})"
            else:
                reason = (result.error if result and result.error else "") or "не скачался"
            self._failures[item.url] = reason[:80]
        if not good:
            return False
        before = self.progress.files_sent if self.progress else 0
        refused = await self._deliver(job, album, good, results, caption)
        for url, reason in refused.items():
            self._failures[url] = reason
        return bool(self.progress and self.progress.files_sent > before)

    async def _deliver(
        self,
        job: Job,
        album: AlbumData,
        batch: list[FileJob],
        results: dict[str, DownloadResult],
        caption: str,
    ) -> dict[str, str]:
        """Send one batch through the userbot. Returns {file url: reason} for files that stayed undelivered."""
        progress = self.progress
        assert progress is not None
        progress.stage = "готовлю файлы"
        keys = {item.url: file_key(item.url) for item in batch}
        url_by_key = {key: url for url, key in keys.items()}
        prepared = await asyncio.gather(
            *(
                prepare_item(results[item.url].path, keys[item.url], random_id_for(job.id, keys[item.url]))
                for item in batch
            )
        )
        remaining: list[SendItem] = list(prepared)
        caption_used = False  # the title goes out once, with the first message that really arrived
        undelivered: dict[str, str] = {}
        attempts = 0
        transient_streak = 0
        one_by_one = False  # after the whole batch kept failing: find the file that spoils it

        def on_delivered(done: list[SendItem]) -> None:
            nonlocal attempts, transient_streak, caption_used
            attempts = transient_streak = 0
            caption_used = True
            nbytes = sum(item.size for item in done)
            self.state.record_batch(album.url, [item.key for item in done], nbytes)
            job.files_sent += len(done)
            job.bytes_sent += nbytes
            progress.files_sent += len(done)
            progress.bytes_delivered += nbytes
            delivered = {item.key for item in done}
            remaining[:] = [item for item in remaining if item.key not in delivered]
            self._discard_files(done)

        while remaining:
            if stop_requested():
                raise StopRequested
            await self._wait_for_telegram()
            progress.stage = "отправляю"
            chunk = [remaining[0]] if one_by_one else list(remaining)
            try:
                refused = await self.userbot.send_batch(
                    chunk,
                    "" if caption_used else caption,
                    on_delivered=on_delivered,
                    on_bytes=progress.add_upload,
                )
            except RetryLater as exc:
                await self._pause(exc.seconds, f"Telegram просит подождать {int(exc.seconds)} с")
                continue
            except DestinationError as exc:
                await self._block(describe_error(exc))
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                link_up = self.userbot.ready
                if (isinstance(exc, TRANSIENT_ERRORS) or not link_up) and transient_streak < MAX_TRANSIENT_STREAK:
                    if link_up:  # a dead link is waited for without limit, a live one that keeps failing is not
                        transient_streak += 1
                    LOG.warning("Sending is interrupted (%s: %s); repeating", type(exc).__name__, exc)
                    await self._pause(TRANSIENT_PAUSE_SECONDS, "нет связи с Telegram")
                    continue
                attempts += 1
                LOG.warning("Sending failed (%d/%d): %s: %s", attempts, MAX_SEND_ATTEMPTS, type(exc).__name__, exc)
                if attempts < MAX_SEND_ATTEMPTS:
                    await self._pause(5 * attempts, "повторяю отправку")
                    continue
                attempts = transient_streak = 0
                if not one_by_one and len(remaining) > 1:
                    LOG.warning("The batch keeps failing: sending its %d files one at a time", len(remaining))
                    one_by_one = True
                    continue
                reason = f"{type(exc).__name__}: {exc}"[:80]
                for item in chunk:  # a single file that cannot be sent no longer holds the others back
                    undelivered[url_by_key[item.key]] = reason
                dropped = {item.key for item in chunk}
                remaining[:] = [item for item in remaining if item.key not in dropped]
                continue
            for key, reason in refused.items():
                undelivered[url_by_key[key]] = f"Telegram: {reason}"[:80]
            handled = {item.key for item in chunk}
            remaining[:] = [item for item in remaining if item.key not in handled]
            attempts = transient_streak = 0
        return undelivered

    def _discard_files(self, items: list[SendItem]) -> None:
        """Delete what was posted (the media only when deletion is on; thumbnails are always scratch files)."""
        for item in items:
            paths = item.cleanup_paths() if self.settings.delete_after_upload else [item.thumb]
            for path in paths:
                if path is None:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    LOG.warning("Could not delete %s: %s", path, exc)

    # ----- waiting ---------------------------------------------------------------------------------

    async def _wait_for_telegram(self) -> None:
        while not self.userbot.ready:
            if stop_requested():
                raise StopRequested
            self.waiting = f"Telegram: {self.userbot.status_text()}"
            await self.userbot.wait_ready(timeout=5)
        self.waiting = ""

    async def _sleep(self, seconds: float) -> None:
        """Sleep in short slices so that a stop request (Q) ends the wait within a second."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            if stop_requested():
                raise StopRequested
            await asyncio.sleep(min(1.0, left))

    async def _pause(self, seconds: float, reason: str) -> None:
        self.waiting = reason
        try:
            await self._sleep(seconds)
        finally:
            self.waiting = ""

    async def _block(self, reason: str) -> None:
        """The target chat refuses our messages: keep the queue and look again in a minute."""
        self.blocked = reason
        if reason != self._last_blocked:
            self._last_blocked = reason
            log_error(f"Нет доступа к группе/теме: {reason}. Проверю снова через минуту")
        try:
            await self._sleep(DESTINATION_RETRY_SECONDS)
        finally:
            self.blocked = ""

    # ----- housekeeping ----------------------------------------------------------------------------

    def _sweep_staging(self) -> None:
        """Remove staging folders of albums that are no longer queued (only when files are deleted after upload)."""
        if not self.settings.delete_after_upload or not self.staging.is_dir():
            return
        live = {code_of(job.url) for job in self.state.queue if job.kind == "album"}
        for folder in self.staging.iterdir():
            match = _STAGING_CODE_RE.search(folder.name)
            if folder.is_dir() and (match is None or match.group(1) not in live):
                shutil.rmtree(folder, ignore_errors=True)
