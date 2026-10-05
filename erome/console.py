import asyncio
import re
import shutil
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from colorama import Fore, Style, init
from tqdm import tqdm

from erome.config import (
    BAR_FORMAT,
    BASE_DIR,
    MAX_LOG_MESSAGE,
    MAX_PROGRESS_WIDTH,
    RETRYABLE_STATUSES,
)


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
