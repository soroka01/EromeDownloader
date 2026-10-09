"""Terminal UI: colours, one live progress line, prompts and hot keys (no CLI arguments)."""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager

RESET = '\x1b[0m'
STYLES = {'dim': '\x1b[90m', 'ok': '\x1b[32m', 'warn': '\x1b[33m', 'error': '\x1b[31m',
          'title': '\x1b[1;36m', 'key': '\x1b[1;33m', 'bold': '\x1b[1m'}
# Hot keys must work on a Russian keyboard layout as well.
KEY_ALIASES = {'й': 'q', 'ы': 's', 'т': 'n', 'з': 'p', 'р': 'h'}
BAR_WIDTH = 16


def human_size(num: float) -> str:
    value = float(num)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if abs(value) < 1024:
            return f'{value:.0f} {unit}' if unit == 'Б' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} ТБ'


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def plural(count: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 файл, 2 файла, 5 файлов."""
    if 11 <= count % 100 <= 14:
        return many
    return one if count % 10 == 1 else few if 2 <= count % 10 <= 4 else many


def shorten(text: str, limit: int = 70) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _enable_ansi() -> bool:
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Console:
    """Prints log lines above a single live status line (activity + transfer progress)."""

    def __init__(self, stream=None):
        self._stream = stream
        self._ansi = None
        self._lock = threading.RLock()
        self._shown = 0
        self._activity = ''
        self._started = 0.0
        self._transfer = None
        self._drawn_at = 0.0

    @property
    def out(self):
        return self._stream or sys.stdout

    @property
    def tty(self) -> bool:
        try:
            return bool(self.out.isatty())
        except (AttributeError, ValueError):
            return False

    @property
    def color(self) -> bool:
        if self._ansi is None:
            self._ansi = self.tty and 'NO_COLOR' not in os.environ and _enable_ansi()
        return self._ansi and self.tty

    def paint(self, text: str, style: str | None) -> str:
        return f'{STYLES[style]}{text}{RESET}' if style and self.color else text

    def set_title(self, text: str) -> None:
        if os.name == 'nt' and self.tty:
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleTitleW(text)
            except Exception:
                pass

    def _write(self, text: str) -> None:
        stream = self.out
        try:
            stream.write(text)
            stream.flush()
        except UnicodeEncodeError:
            encoding = getattr(stream, 'encoding', None) or 'utf-8'
            stream.write(text.encode(encoding, 'replace').decode(encoding))
            stream.flush()
        except (OSError, ValueError):
            pass

    # ----- plain output -------------------------------------------------------------------

    def line(self, text: str = '', style: str | None = None) -> None:
        with self._lock:
            self._clear()
            self._write(self.paint(text, style) + '\n')
            self._draw(force=True)

    def title(self, text: str) -> None:
        self.line()
        self.line(text, 'title')
        self.line('─' * min(len(text), 72), 'dim')

    def field(self, label: str, value: object, style: str | None = None) -> None:
        self.line(f'  {self.paint(f"{label:<12}", "dim")}{self.paint(str(value), style)}')

    # ----- live status line ---------------------------------------------------------------

    def activity(self, text: str) -> None:
        """Show what is happening right now; replaces the previous status line."""
        with self._lock:
            self._activity = text
            self._started = time.monotonic()
            self._transfer = None
            self._draw(force=True)

    def transfer(self, done: float, total: float = 0) -> None:
        with self._lock:
            self._transfer = (done, total)
            self._draw()

    def idle(self) -> None:
        with self._lock:
            self._activity = ''
            self._transfer = None
            self._clear()

    @contextmanager
    def task(self, text: str):
        """Live status line for the duration of a block."""
        self.activity(text)
        try:
            yield
        finally:
            self.idle()

    def _render(self) -> str:
        text = f'  {self._activity}'
        if self._transfer is None:
            return text
        done, total = self._transfer
        elapsed = max(time.monotonic() - self._started, 1e-6)
        speed = done / elapsed
        parts = [text]
        if total > 0:
            ratio = min(done / total, 1.0)
            filled = int(ratio * BAR_WIDTH)
            parts.append(f'[{"█" * filled}{"░" * (BAR_WIDTH - filled)}] {ratio * 100:3.0f}%')
            parts.append(f'{human_size(done)} / {human_size(total)}')
        else:
            parts.append(human_size(done))
        if elapsed >= 1 and done:
            parts.append(f'{human_size(speed)}/с')
        if total > done and speed > 0 and elapsed >= 2:
            parts.append(f'осталось {human_duration((total - done) / speed)}')
        return '  '.join(parts)

    def _draw(self, force: bool = False) -> None:
        if not self._activity or not self.tty:
            return
        now = time.monotonic()
        if not force and now - self._drawn_at < 0.1:
            return
        self._drawn_at = now
        width = max(shutil.get_terminal_size((100, 24)).columns - 1, 20)
        text = self._render()
        if len(text) > width:
            text = text[:width - 1] + '…'
        padding = ' ' * max(0, self._shown - len(text))
        self._write('\r' + self.paint(text, 'dim') + padding)
        self._shown = len(text)

    def _clear(self) -> None:
        if self._shown and self.tty:
            self._write('\r' + ' ' * self._shown + '\r')
        self._shown = 0

    # ----- prompts ------------------------------------------------------------------------

    def ask(self, prompt: str, default: object = None) -> str:
        self.idle()
        suffix = f' [{default}]' if default not in (None, '') else ''
        answer = input(f'{self.paint(prompt, "bold")}{suffix}: ').strip()
        return answer if answer else ('' if default is None else str(default))

    def ask_int(self, prompt: str, default: int = 0) -> int:
        while True:
            answer = self.ask(prompt, default)
            try:
                value = int(answer)
            except ValueError:
                value = -1
            if value >= 0:
                return value
            self.line('  Нужно целое число 0 или больше.', 'warn')

    def confirm(self, prompt: str, default: bool = False) -> bool:
        answer = self.ask(f'{prompt} ({"Д/н" if default else "д/Н"})').lower()
        if not answer:
            return default
        return answer in {'д', 'да', 'y', 'yes', '1'}

    def pause(self, prompt: str = 'Нажмите Enter, чтобы продолжить') -> None:
        try:
            self.ask(prompt)
        except (EOFError, KeyboardInterrupt):
            pass

    # ----- hot keys -----------------------------------------------------------------------

    def poll_key(self) -> str | None:
        """Return one pressed key (lower-case, Latin layout) without blocking, or None."""
        try:
            if os.name == 'nt':
                import msvcrt
                if not msvcrt.kbhit():
                    return None
                char = msvcrt.getwch()
                if char in ('\x00', '\xe0'):  # function/arrow key: discard its second code
                    msvcrt.getwch()
                    return None
            else:
                import select
                if not select.select([sys.stdin], [], [], 0)[0]:
                    return None
                char = (sys.stdin.readline() or ' ')[:1]
        except (OSError, ValueError, ImportError):
            return None
        char = char.lower()
        return KEY_ALIASES.get(char, char)


class ConsoleHandler(logging.Handler):
    """Shows INFO+ records as `HH:MM:SS message`; warnings and errors are coloured."""

    def __init__(self, console: Console):
        super().__init__(logging.INFO)
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno >= logging.ERROR:
                style, tag = 'error', 'ОШИБКА: '
            elif record.levelno >= logging.WARNING:
                style, tag = 'warn', 'Внимание: '
            else:
                style, tag = ('ok' if getattr(record, 'ok', False) else None), ''
            stamp = time.strftime('%H:%M:%S', time.localtime(record.created))
            self.console.line(f'{stamp}  {tag}{record.getMessage()}', style)
        except Exception:
            self.handleError(record)


ui = Console()
