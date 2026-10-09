import asyncio
import time
from pathlib import Path
from typing import Callable, Iterable

import aiofiles
import aiohttp

from erome.config import (
    CHUNK_SIZE,
    IDLE_TIMEOUT,
    MAX_ATTEMPTS,
    RETRYABLE_STATUSES,
    UNAVAILABLE_STATUSES,
)
from erome.console import (
    friendly_error,
    is_quiet_retry_error,
    log,
    log_error,
    log_retry_unless_quiet,
    log_success,
    log_warn,
    short_url,
)
from erome.models import (
    DownloadError,
    DownloadResult,
    DownloadSummary,
    FileJob,
    FileResultCallback,
)
from erome.net import (
    drain_tasks,
    make_session,
    optional_progress,
    parse_content_length,
    parse_content_range_total,
)
from erome.storage import ensure_runtime_dirs, record_file_results
from erome.ui import human_size
from erome.urls import build_download_jobs


def _chain_bytes(
    first: Callable[[int], None], second: Callable[[int], None] | None
) -> Callable[[int], None]:
    if second is None:
        return first

    def both(count: int) -> None:
        first(count)
        second(count)

    return both


async def _download(
    album: str,
    urls: Iterable[str],
    max_connections: int,
    download_path: Path,
    desc: str = "Альбом",
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
    headers = {"Accept": "*/*", "Connection": "keep-alive", "Referer": album}

    results: list[DownloadResult] = []
    semaphore = asyncio.Semaphore(max_connections)

    def on_result(result: DownloadResult) -> None:
        results.append(result)
        if file_result_callback:
            file_result_callback(result)

    async with make_session(max_connections, headers) as session:
        with optional_progress(
            show_progress,
            label=desc,
            total=len(jobs),
            unit="файлов",
        ) as album_progress:
            on_bytes = byte_progress
            if album_progress is not None:
                on_bytes = _chain_bytes(album_progress.add_bytes, byte_progress)

            tasks = [
                asyncio.create_task(
                    _download_file(
                        session,
                        job,
                        semaphore,
                        show_messages=show_summary,
                        byte_progress=on_bytes,
                    )
                )
                for job in jobs
            ]
            await drain_tasks(tasks, on_result, album_progress)

    summary = DownloadSummary(results=results)
    record_file_results(album, download_path, results)
    if show_summary:
        if summary.failed_count or summary.banned_count:
            log_warn(
                f"готово {summary.ok_count}/{summary.total}, "
                f"ошибок {summary.failed_count}, "
                f"недоступно {summary.banned_count}, "
                f"{human_size(summary.total_size)}"
            )
        else:
            log_success(
                f"{download_path.name}: {summary.ok_count}/{summary.total} файлов, "
                f"{human_size(summary.total_size)}"
            )
    return summary


async def _download_file(
    session: aiohttp.ClientSession,
    job: FileJob,
    semaphore: asyncio.Semaphore,
    show_messages: bool,
    byte_progress: Callable[[int], None] | None,
) -> DownloadResult:
    part_path = job.path.with_name(job.path.name + ".part")
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with semaphore:
                return await _download_file_once(
                    session,
                    job,
                    part_path,
                    attempt,
                    show_messages=show_messages,
                    byte_progress=byte_progress,
                )
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
                    log_success(f"{job.path.name} докачан ранее ({human_size(size)})")
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
            total_size = remote_total or resume_from + content_length
        else:
            if resume_from and response.status == 200:
                if show_messages:
                    log(f"{job.path.name}: сервер не дал докачку, перекачиваю")
            mode = "wb"
            total_size = content_length

        if job.path.exists() and total_size and job.path.stat().st_size == total_size:
            if part_path.exists():
                part_path.unlink()
            if show_messages:
                log_success(f"{job.path.name} уже скачан ({human_size(total_size)})")
            return DownloadResult(
                job.url,
                "skipped",
                job.path,
                attempts=attempt,
                size_bytes=total_size,
            )

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
