"""Console output: thin wrappers over `erome.ui`, the aggregate live progress line and hot keys."""
import asyncio
import re
import sys
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from erome.config import BASE_DIR, MAX_LOG_MESSAGE, RETRYABLE_STATUSES
from erome.ui import BAR_WIDTH, human_duration, human_size, ui


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


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


# ----- log lines ----------------------------------------------------------------------------


def log(message: str, style: str | None = None) -> None:
    """One log line `HH:MM:SS  message` above the live status line."""
    ui.line(f"{datetime.now():%H:%M:%S}  {message}", style)


def log_hint(message: str) -> None:
    log(message, "dim")


def log_success(message: str) -> None:
    log(message, "ok")


def log_warn(message: str) -> None:
    log(f"Внимание: {message}", "warn")


def log_error(message: str) -> None:
    log(f"ОШИБКА: {message}", "error")


def log_retry(message: str) -> None:
    log(message, "warn")


def log_auto(message: str) -> None:
    log_hint(message)


def log_skip(message: str) -> None:
    log_hint(message)


def log_queue(message: str) -> None:
    log_hint(message)


def log_done(message: str, style: str | None = "ok") -> None:
    log(message, style)


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


def print_boxed(text: str, style: str | None = None) -> None:
    """Banner: the first line is the title, the other lines are details."""
    first, *rest = text.split("\n")
    ui.title(first)
    for line in rest:
        ui.line(line, style or "dim")


# ----- live progress line -------------------------------------------------------------------


_active: list["LiveProgress"] = []


class LiveProgress:
    """Aggregate progress (items, files, bytes, speed, ETA) drawn on the single `ui` status line.

    Used as `with LiveProgress(...) as progress:` inside a running event loop. All updates come
    from that loop and `ui` is the only writer of the terminal line (no tqdm).
    """

    def __init__(self, label: str, total: int = 0, unit: str = "элементов", total_bytes: int = 0):
        self.label = label
        self.total = total
        self.unit = unit
        self.total_bytes = total_bytes
        self.done = 0
        self.bytes = 0
        self.files_done = 0
        self.files_total = 0
        self.note = ""
        self.extra: Callable[[], str] | None = None
        self._speed = DownloadSpeed()
        self._started = time.monotonic()
        self._ticker: asyncio.Task | None = None

    # ----- updates

    def update(self, count: int = 1) -> None:
        self.done += count
        self._draw()

    def add_file(self, count: int = 1) -> None:
        self.files_done += count
        self._draw()

    def add_bytes(self, count: int) -> None:
        self.bytes += count
        self._speed.add(count)

    # ----- rendering

    def text(self) -> str:
        parts = [self.label]
        if self.total:
            if self.files_total:
                ratio = min(self.files_done / self.files_total, 1.0)
            else:
                ratio = min(self.done / self.total, 1.0)
            filled = int(ratio * BAR_WIDTH)
            parts.append(f"[{'█' * filled}{'░' * (BAR_WIDTH - filled)}] {ratio * 100:3.0f}%")
            parts.append(f"{self.done}/{self.total} {self.unit}")
        elif self.done:
            parts.append(f"{self.done} {self.unit}")
        if self.files_total:
            parts.append(f"файлов {self.files_done}/{self.files_total}")
        if self.bytes:
            parts.append(
                f"{human_size(self.bytes)} / {human_size(self.total_bytes)}"
                if self.total_bytes > self.bytes
                else human_size(self.bytes)
            )
            rate = self._speed.rate()
            if rate:
                parts.append(f"{human_size(rate)}/с")
        eta = self._eta()
        if eta is not None:
            parts.append(f"осталось {human_duration(eta)}")
        if self.extra:
            extra = self.extra()
            if extra:
                parts.append(extra)
        if self.note:
            parts.append(self.note)
        if _control is not None and _control.paused:
            parts.append("ПАУЗА (P — продолжить)")
        return "  ".join(parts)

    def _eta(self) -> float | None:
        elapsed = time.monotonic() - self._started
        if elapsed < 2:
            return None
        if self.total_bytes > self.bytes > 0:
            rate = self._speed.rate()
            if rate > 0:
                return (self.total_bytes - self.bytes) / rate
        if self.total > self.done > 0:
            return elapsed / self.done * (self.total - self.done)
        return None

    def _draw(self) -> None:
        if _active and _active[-1] is self:
            ui.activity(self.text())

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            self._draw()

    # ----- context manager

    def __enter__(self) -> "LiveProgress":
        _active.append(self)
        self._started = time.monotonic()
        self._draw()
        try:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())
        except RuntimeError:
            self._ticker = None
        return self

    def __exit__(self, *_exc) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        if self in _active:
            _active.remove(self)
        if _active:
            _active[-1]._draw()
        else:
            ui.idle()


def progress_snapshot() -> str:
    return _active[-1].text() if _active else ""


# ----- hot keys -----------------------------------------------------------------------------


HOTKEYS = (
    "Клавиши: [S] статус · [P] пауза · [Q] остановить после текущего элемента · "
    "Ctrl+C — прервать сразу"
)
_control: "RunControl | None" = None


class RunControl:
    """Hot-key state of the running action: pause and stop between items."""

    def __init__(self):
        self.stop = False
        self.paused = False

    def handle(self, key: str) -> None:
        if key == "q":
            if not self.stop:
                log("Остановка запрошена: новые элементы не запускаются, текущие дойдут до конца…")
            self.stop = True
        elif key == "p":
            self.paused = not self.paused
            log(
                "Пауза: новые элементы не запускаются, текущие дойдут до конца"
                if self.paused
                else "Продолжаю работу"
            )
        elif key == "s":
            status = progress_snapshot()
            ui.line(f"  {status}" if status else "  Сейчас идёт сбор данных или пауза между шагами.", "dim")
        elif key in ("h", "?"):
            ui.line(HOTKEYS, "dim")


async def listen_keys(control: RunControl) -> None:
    while True:
        key = ui.poll_key()
        if key:
            try:
                control.handle(key)
            except Exception:
                pass
        await asyncio.sleep(0.1)


@asynccontextmanager
async def hotkeys():
    """Enable hot keys (interactive terminal only) for the duration of one running action."""
    global _control
    control = RunControl()
    _control = control
    task = None
    if sys.stdin is not None and sys.stdin.isatty():
        ui.line(HOTKEYS, "dim")
        task = asyncio.create_task(listen_keys(control))
    try:
        yield control
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        _control = None


def stop_requested() -> bool:
    return _control is not None and _control.stop


async def checkpoint() -> bool:
    """Wait while paused; return True when the user asked to stop before the next item."""
    control = _control
    if control is None:
        return False
    while control.paused and not control.stop:
        await asyncio.sleep(0.2)
    return control.stop


async def with_hotkeys(coro):
    async with hotkeys():
        return await coro


