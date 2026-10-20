"""Running the Telegram mode: one process, one lock, the log files, the console, and the tasks that make it work.

`serve` starts everything in one event loop: bot polling (aiogram), the userbot connection (Telethon), the job worker,
the live panels and the console status line; `run_telegram_bot` is the menu entry that prepares settings and sign-in
first. The bot itself (panel, commands, buttons) is in tg_bot.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.types import BotCommand

from erome.config import BASE_DIR
from erome.console import RunControl, configure_console, hotkeys, log, log_done, log_error, log_success, log_warn
from erome.storage import ensure_runtime_dirs
from erome.tg_bot import BotApp, build_router, count_text
from erome.tg_pipeline import Pipeline
from erome.tg_settings import (
    SETTINGS_PATH,
    SettingsError,
    TelegramSettings,
    bot_token_is_set,
    ensure_settings_file,
    load_settings,
    read_raw,
    save_bot_token,
    valid_bot_token,
)
from erome.tg_state import TelegramState
from erome.tg_transport import clean_text, cryptg_available, sign_in_if_needed
from erome.tg_userbot import Userbot
from erome.ui import ConsoleHandler, ui

LOG = logging.getLogger("erome.tg")

DATA_DIR = BASE_DIR / "data"


class TelegramControl(RunControl):
    """Console hot keys of the Telegram mode: S prints what is going on instead of the downloader's bar."""

    def __init__(self) -> None:
        super().__init__()
        self.status: Callable[[], str] | None = None

    def handle(self, key: str) -> None:
        if key == "s" and self.status is not None:
            ui.line(f"  {self.status()}", "dim")
        elif key == "p":
            self.paused = not self.paused
            log("Пауза: текущая пачка файлов дойдёт до конца, дальше ничего не начнётся" if self.paused else "Продолжаю работу")
        elif key == "q":
            if not self.stop:
                log("Остановка запрошена: текущая пачка файлов дойдёт до конца, очередь сохранится до следующего запуска…")
            self.stop = True
        else:
            super().handle(key)



class InstanceLock:
    """Only one Telegram-mode process may run: two would fight over the bot's updates and the session file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None


class PollingWatch(logging.Handler):
    """Tells the console when the bot stops receiving messages.

    aiogram retries a failing getUpdates forever and says so only in its log: with a revoked token or a dead
    connection the console would keep showing «waiting for links». The alarm comes after a few failures in a row
    (a short hiccup stays quiet) and is repeated at most once an hour.
    """

    FAILURES_BEFORE_ALARM = 6
    REPEAT_SECONDS = 3600.0

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.failures = 0
        self.alarmed_at: float | None = None

    @staticmethod
    def explain(message: str) -> str:
        if "TelegramUnauthorizedError" in message:
            return "токен бота отозван или неверен: получите новый у @BotFather и впишите bot_token в telegram.json"
        if "TelegramConflictError" in message:
            return "этот же бот уже запущен где-то ещё (одним токеном нельзя читать сообщения из двух мест)"
        return message.removeprefix("Failed to fetch updates - ")[:120] or "нет связи с Telegram"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if message.startswith("Failed to fetch updates"):
                self.failures += 1
                now = time.monotonic()
                due = self.alarmed_at is None or now - self.alarmed_at >= self.REPEAT_SECONDS
                if self.failures >= self.FAILURES_BEFORE_ALARM and due:
                    self.alarmed_at = now
                    log_error(f"Бот не получает сообщения: {self.explain(message)}. Программа продолжает пытаться")
            elif message.startswith("Connection established"):
                if self.alarmed_at is not None:
                    log_success("Бот снова получает сообщения")
                self.failures, self.alarmed_at = 0, None
        except Exception:  # a watcher must never disturb the logging call it listens to
            self.handleError(record)


def configure_logging() -> None:
    """File log in data/ (Telegram's own warnings included) and short lines in the console."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%d.%m.%y %H:%M:%S")
    file_handler = RotatingFileHandler(DATA_DIR / "telegram.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    ours = logging.getLogger("erome.tg")
    ours.setLevel(logging.INFO)
    ours.propagate = False
    for handler in list(ours.handlers):
        ours.removeHandler(handler)
        handler.close()
    ours.addHandler(file_handler)
    ours.addHandler(ConsoleHandler(ui))
    for name in ("telethon", "aiogram", "aiohttp"):
        third = logging.getLogger(name)
        third.setLevel(logging.WARNING)
        third.propagate = False
        for handler in list(third.handlers):
            third.removeHandler(handler)
        third.addHandler(file_handler)
    polling = logging.getLogger("aiogram.dispatcher")  # the polling loop reports its failures and recovery here
    polling.setLevel(logging.INFO)
    for handler in list(polling.handlers):
        polling.removeHandler(handler)
    polling.addHandler(PollingWatch())


@contextlib.contextmanager
def console_selection_off():
    """Windows consoles freeze the whole program while text is selected with the mouse (QuickEdit mode).

    A bot that must keep polling and uploading cannot stand a stray click, so the mode is switched off for
    the duration of the run and restored afterwards. Anything unusual (not a console, not Windows) is ignored.
    """
    restore = None
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32")  # a private handle: the prototypes below must not leak into ui.py
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint32()
            if handle and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                extended, quick_edit = 0x0080, 0x0040
                if kernel32.SetConsoleMode(handle, (mode.value | extended) & ~quick_edit):
                    restore = (kernel32, handle, mode.value | extended)  # extended flag: the original QuickEdit bit counts
        except Exception:
            restore = None
    try:
        yield
    finally:
        if restore is not None:
            with contextlib.suppress(Exception):
                restore[0].SetConsoleMode(restore[1], restore[2])


async def announce_connection(userbot: Userbot) -> None:
    """Tell the console when the Telegram link comes up or goes down (the bot panel shows the same)."""
    was_ready = False
    while True:
        await asyncio.sleep(1.0)
        try:
            ready = userbot.ready
            if ready and not was_ready:
                premium = " · Premium" if userbot.premium else ""
                place = f"«{clean_text(userbot.chat_title)}»"
                if userbot.topic_title:
                    place += f", тема «{clean_text(userbot.topic_title)}»"
                log_success(f"Telegram подключён: {userbot.account_name}{premium}; группа {place}")
            elif was_ready and not ready:
                log_warn(f"Telegram: {userbot.status_text()}")
            was_ready = ready
        except Exception as exc:  # cosmetic: never end the mode because of an announcement
            LOG.debug("Connection announcement failed: %s", exc)


async def console_status_loop(app: BotApp) -> None:
    while True:
        try:
            ui.activity(app.console_line())
        except Exception as exc:  # cosmetic: never end the mode because of the status line
            LOG.debug("Console status failed: %s", exc)
        await asyncio.sleep(1.0)


async def serve(settings: TelegramSettings, control: TelegramControl) -> None:
    ensure_runtime_dirs()
    state = TelegramState()
    state.load()
    userbot = Userbot(settings)
    pipeline = Pipeline(state, userbot, settings)
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    app = BotApp(bot, state, pipeline, userbot, settings, control)
    control.status = app.status_text
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(app))

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SettingsError("Telegram не принял токен бота: проверьте bot_token в telegram.json") from None
    except BaseException:
        await bot.session.close()
        raise
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="как пользоваться"),
                BotCommand(command="queue", description="показать панель очереди"),
                BotCommand(command="resend", description="отправить альбом заново"),
            ]
        )
    except TelegramAPIError as exc:
        LOG.warning("Could not register the bot commands: %s", exc)
    queued = len(state.queued())
    log_success(f"Бот @{me.username} запущен. Владельцы: {', '.join(str(i) for i in sorted(settings.owner_ids))}")
    if queued:
        log(f"Продолжаю очередь: {count_text(queued, 'ссылка', 'ссылки', 'ссылок')}")
    if not cryptg_available():
        log_warn("Не установлен cryptg: отправка больших файлов будет медленной и может подвешивать программу")

    await userbot.start()
    tasks = {
        "pipeline": asyncio.create_task(pipeline.run(), name="pipeline"),
        "panels": asyncio.create_task(app.refresh_loop(), name="panels"),
        "console": asyncio.create_task(console_status_loop(app), name="console"),
        "link": asyncio.create_task(announce_connection(userbot), name="link"),
        "polling": asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=["message", "callback_query"],
            ),
            name="polling",
        ),
    }
    try:
        done, _ = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # a crashed part ends the mode with its error; a finished worker means "stop"
    finally:
        ui.idle()
        pipeline.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(tasks["pipeline"], 10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(dispatcher.stop_polling(), 5)
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(userbot.stop(), 15)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bot.session.close(), 5)
        state.save()


async def _run(settings: TelegramSettings) -> None:
    control = TelegramControl()
    async with hotkeys(control):
        await serve(settings, control)


def _ask_token() -> bool:
    ui.line("Нужен токен Telegram-бота: создайте бота у @BotFather (команда /newbot) и вставьте токен сюда.")
    ui.line("Он сохранится в telegram.json (этот файл не попадает в git).", "dim")
    token = ui.ask("Токен бота (пусто — отмена)")
    if not token:
        return False
    if not valid_bot_token(token):
        log_error("Это не похоже на токен бота (ожидается вид 123456789:AAH…). Попробуйте ещё раз.")
        return False
    save_bot_token(token)
    return True


def run_telegram_bot() -> None:
    """Menu entry: prepare settings and the userbot sign-in in the console, then run until stopped."""
    configure_console()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(DATA_DIR / "telegram.lock")
    if not lock.acquire():
        log_error("Telegram-режим уже запущен в другом окне: закройте его или дождитесь остановки.")
        return
    try:
        configure_logging()
        if ensure_settings_file():
            log_warn(f"Создан файл {SETTINGS_PATH.name}: впишите в него chat_id, thread_id, owner_ids и учётные данные.")
        try:
            if not bot_token_is_set(read_raw()) and not _ask_token():
                return
            settings = load_settings()
        except SettingsError as exc:
            log_error(str(exc))
            log(f"Файл настроек: {SETTINGS_PATH}", "dim")
            return
        ui.idle()
        signed_in, info = sign_in_if_needed(settings, say=ui.line)
        if not signed_in:
            log_error(f"Отправка невозможна: {info}")
            return
        log_success(f"Аккаунт для отправки: {info}")
        if settings.account_id is None:
            log_warn("В telegram.json не указан account_id: проверки аккаунта нет")
        try:
            with console_selection_off():
                asyncio.run(_run(settings))
        except SettingsError as exc:
            log_error(str(exc))
        else:
            log_done("Telegram-режим остановлен. Очередь сохранена — продолжится при следующем запуске.")
    finally:
        ui.idle()
        lock.release()
