"""The Telegram bot of the Telegram mode: takes Erome links from the owner, shows one live panel, runs the worker.

One process hosts everything: bot polling (aiogram), the userbot connection (Telethon), the job worker and the
console status line. Links are queued durably before the owner's message is deleted, so nothing sent to the bot
can get lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from erome.config import BASE_DIR
from erome.console import RunControl, configure_console, hotkeys, log, log_done, log_error, log_success, log_warn
from erome.storage import ensure_runtime_dirs
from erome.tg_pipeline import Pipeline, Progress
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
from erome.tg_state import CANCELLED, DONE, FAILED, PARTIAL, Job, TelegramState, canonical_url
from erome.tg_userbot import STATE_READY, Userbot, clean_text, cryptg_available, sign_in_if_needed
from erome.ui import ConsoleHandler, human_duration, human_size, plural, ui
from erome.urls import link_kind

LOG = logging.getLogger("erome.tg")

DATA_DIR = BASE_DIR / "data"
MSK = timezone(timedelta(hours=3), "МСК")
PANEL_EDIT_INTERVAL = 3.5  # seconds between edits of the live panel (Telegram allows about one per second)
NOTICE_SECONDS = 25
CONFIRM_SECONDS = 6
QUEUE_LINES = 5
RECENT_LINES = 5
PANEL_LIMIT = 3900

_LINK_RE = re.compile(r"(?<![\w.@/-])(?:https?://)?(?:www\.)?erome\.com/[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ").,;:!?]}>\"'"

HELP = (
    "Пришлите ссылку на <b>альбом</b> (erome.com/a/…) или на <b>профиль</b> (erome.com/имя) — "
    "можно сразу несколько: через пробел, с новой строки или отдельными сообщениями. "
    "Всё скачается и уйдёт в группу по очереди.\n"
    "Профиль: добавлю его альбомы, которых ещё не отправляли, от старых к новым. "
    "Повторная ссылка на альбом ничего не продублирует — отправится только недостающее.\n"
    "Выложить альбом заново: <code>/resend ссылка</code>."
)


# ----- helpers -------------------------------------------------------------------------------------


def esc(text: object) -> str:
    """Text for Telegram's HTML mode (only &, < and > need escaping)."""
    return html.escape(str(text), quote=False)


def attr(text: object) -> str:
    return esc(text).replace('"', "&quot;")


def extract_links(message: Message) -> list[tuple[str, str]]:
    """(kind, url) of every Erome album/profile link in a message, in order, without duplicates."""
    text = message.text or message.caption or ""
    candidates: list[str] = []
    for entity in (message.entities or message.caption_entities or []):
        if entity.type == "text_link" and entity.url:
            candidates.append(entity.url)
    candidates.extend(match.group(0) for match in _LINK_RE.finditer(text))
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in candidates:
        raw = raw.strip().rstrip(_TRAILING)
        if not raw.lower().startswith("http"):
            raw = f"https://{raw}"
        url = canonical_url(raw)
        kind = link_kind(url)
        if kind in ("album", "account") and url not in seen:
            seen.add(url)
            found.append((kind, url))
    return found


def msk_time(stamp: float | None = None) -> str:
    moment = datetime.fromtimestamp(stamp, MSK) if stamp else datetime.now(MSK)
    return moment.strftime("%d.%m.%y %H:%M:%S")


def link(title: str, url: str, limit: int = 60) -> str:
    title = " ".join(str(title).split()) or url
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "…"
    return f'<a href="{attr(url)}">{esc(title)}</a>'


def job_label(job: Job) -> str:
    if job.kind == "account":
        return link(job.title or "Профиль", job.url)
    return link(job.title or job.url.replace("https://", ""), job.url)


def count_text(count: int, one: str, few: str, many: str) -> str:
    return f"{count} {plural(count, one, few, many)}"


def view_signature(text: str, markup: InlineKeyboardMarkup) -> tuple:
    return text, tuple(button.text for row in markup.inline_keyboard for button in row)


# ----- the application --------------------------------------------------------------------------------


class TelegramControl(RunControl):
    """Console hot keys of the Telegram mode: S prints what is going on instead of the downloader's bar."""

    def __init__(self) -> None:
        super().__init__()
        self.status: Callable[[], str] | None = None

    def handle(self, key: str) -> None:
        if key == "s" and self.status is not None:
            ui.line(f"  {self.status()}", "dim")
            return
        super().handle(key)


class BotApp:
    def __init__(
        self,
        bot: Bot,
        state: TelegramState,
        pipeline: Pipeline,
        userbot: Userbot,
        settings: TelegramSettings,
        control: RunControl,
    ) -> None:
        self.bot = bot
        self.state = state
        self.pipeline = pipeline
        self.userbot = userbot
        self.settings = settings
        self.control = control
        self.notice = ""
        self.notice_until = 0.0
        self.clear_confirm_until = 0.0
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_view: dict[int, tuple] = {}  # what each panel shows now: (text, button labels)
        self._last_edit: dict[int, float] = {}
        self._denied_logged: set[int] = set()

    # ----- text of the panel ------------------------------------------------------------------------

    def set_notice(self, text: str, seconds: float = NOTICE_SECONDS) -> None:
        self.notice = text
        self.notice_until = time.monotonic() + seconds

    @property
    def paused(self) -> bool:
        return bool(self.control.paused)

    def active(self) -> bool:
        return self.pipeline.progress is not None or bool(self.pipeline.waiting)

    def activity_line(self, progress: Progress) -> str:
        if progress.job.kind == "account":
            return "Читаю список альбомов профиля…"
        parts: list[str] = []
        if progress.files_total:
            parts.append(f"отправлено {progress.files_sent}/{progress.files_total}")
            if progress.files_downloaded < progress.files_total:
                parts.append(f"скачано {progress.files_downloaded}/{progress.files_total}")
        else:
            parts.append(progress.stage)
        if progress.bytes_delivered:
            parts.append(human_size(progress.bytes_delivered))
        rate = progress.upload_speed.rate()
        if rate and progress.stage == "отправляю":
            parts.append(f"{human_size(rate)}/с")
        eta = progress.eta()
        if eta is not None:
            parts.append(f"осталось ≈ {human_duration(eta)}")
        return " · ".join(parts)

    def render(self) -> tuple[str, InlineKeyboardMarkup]:
        now = time.monotonic()
        lines = ["<b>Erome → Telegram</b>"]
        if self.notice and now < self.notice_until:
            lines += ["", self.notice]
        warnings = []
        if self.userbot.state != STATE_READY:
            warnings.append(f"⚠ Telegram: {esc(self.userbot.status_text())}")
        if self.pipeline.blocked:
            warnings.append(f"⚠ Нет доступа к группе или теме: {esc(self.pipeline.blocked)}")
        if self.pipeline.waiting and self.userbot.state == STATE_READY:
            warnings.append(f"⏳ {esc(self.pipeline.waiting)}")
        if self.paused:
            warnings.append("⏸ Пауза: новые альбомы не начинаются")
        if warnings:
            lines += [""] + warnings

        progress = self.pipeline.progress
        queued = self.state.queued()
        if progress is not None:
            job = progress.job
            lines += ["", "<b>Сейчас</b>", job_label(job), esc(self.activity_line(progress))]
        elif not queued:
            lines += ["", "Очередь пуста. Пришлите ссылку на альбом или профиль Erome."]
        if queued:
            lines += ["", f"<b>В очереди</b> ({len(queued)})"]
            for number, job in enumerate(queued[:QUEUE_LINES], 1):
                lines.append(f"{number}. {job_label(job)}")
            if len(queued) > QUEUE_LINES:
                lines.append(f"… и ещё {len(queued) - QUEUE_LINES}")

        recent = list(reversed(self.state.history))[:RECENT_LINES]
        if recent:
            lines += ["", "<b>Недавно</b>"]
            for job in recent:
                icon = {DONE: "✅", PARTIAL: "⚠️", FAILED: "❌", CANCELLED: "🚫"}.get(job.status, "•")
                note = f" — {esc(job.note)}" if job.note else ""
                lines.append(f"{icon} {job_label(job)}{note} · {msk_time(job.finished)}")
        if self.active():
            lines += ["", f"Обновлено {msk_time()}"]

        text = "\n".join(lines)
        while len(text) > PANEL_LIMIT and len(lines) > 6:  # whole lines only: a cut tag would break the HTML
            lines.pop()
            text = "\n".join(lines)
        return text, self.keyboard(bool(queued))

    def keyboard(self, has_queue: bool) -> InlineKeyboardMarkup:
        first = [InlineKeyboardButton(text="▶ Продолжить" if self.paused else "⏸ Пауза", callback_data="tg:pause")]
        if self.pipeline.progress is not None:
            first.append(InlineKeyboardButton(text="✖ Отменить текущий", callback_data="tg:cancel"))
        rows = [first]
        if has_queue:
            confirming = time.monotonic() < self.clear_confirm_until
            label = "⚠ Нажмите ещё раз — очистить" if confirming else f"🗑 Очистить очередь ({len(self.state.queued())})"
            rows.append([InlineKeyboardButton(text=label, callback_data="tg:clear")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ----- the panel message ------------------------------------------------------------------------

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def show_panel(self, chat_id: int, *, fresh: bool = False) -> None:
        """Draw the panel: edit the existing message, or create it (fresh=True moves it to the bottom of the chat)."""
        async with self._lock(chat_id):
            text, markup = self.render()
            message_id = self.state.panel_id(chat_id)
            if fresh and message_id:
                with contextlib.suppress(TelegramAPIError):
                    await self.bot.delete_message(chat_id, message_id)
                message_id = None
            if message_id:
                if await self._edit(chat_id, message_id, text, markup):
                    return
                message_id = None
            sent = await self.bot.send_message(chat_id, text, reply_markup=markup)
            self.state.set_panel(chat_id, sent.message_id)
            self._last_view[chat_id] = view_signature(text, markup)
            self._last_edit[chat_id] = time.monotonic()

    async def _edit(self, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup) -> bool:
        """Edit the panel; False when the message is gone and has to be created again."""
        try:
            await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            return True  # the next refresh repeats the edit
        except TelegramBadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                pass
            elif "not found" in reason or "can't be edited" in reason or "message to edit" in reason:
                return False
            else:
                LOG.warning("Panel edit failed: %s", exc)
        self._last_view[chat_id] = view_signature(text, markup)
        self._last_edit[chat_id] = time.monotonic()
        return True

    async def refresh_loop(self) -> None:
        """Keep every panel up to date: edited only when its text changed, at most every few seconds."""
        while True:
            await asyncio.sleep(1.0)
            try:
                for chat_id_text in list(self.state.panels):
                    await self._refresh_panel(int(chat_id_text))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a cosmetic loop must never take the whole mode down
                LOG.warning("Panel refresh failed: %s: %s", type(exc).__name__, exc)

    async def _refresh_panel(self, chat_id: int) -> None:
        message_id = self.state.panel_id(chat_id)
        if not message_id:
            return
        text, markup = self.render()
        if view_signature(text, markup) == self._last_view.get(chat_id):
            return
        if time.monotonic() - self._last_edit.get(chat_id, 0.0) < PANEL_EDIT_INTERVAL:
            return
        async with self._lock(chat_id):
            if not await self._edit(chat_id, message_id, text, markup):
                self.state.set_panel(chat_id, None)  # deleted by the user: back on their next message

    # ----- console line -------------------------------------------------------------------------------

    def console_line(self) -> str:
        progress = self.pipeline.progress
        queued = len(self.state.queued())
        tail = f" · в очереди {queued}" if queued else ""
        if self.userbot.state != STATE_READY:
            return f"Telegram: {self.userbot.status_text()}{tail}"
        if self.paused:
            return f"Пауза{tail}"
        if progress is None:
            return "Жду ссылки в боте · очередь пуста" if not queued else f"Готовлюсь к следующему{tail}"
        title = progress.title or "…"
        line = f"«{title[:40]}» · {self.activity_line(progress)}"
        if self.pipeline.waiting:
            line += f" · {self.pipeline.waiting}"
        return line + tail

    def status_text(self) -> str:
        """Plain-text status for the S hot key."""
        return self.console_line()


# ----- handlers -----------------------------------------------------------------------------------------


def build_router(app: BotApp) -> Router:
    router = Router()
    owners = app.settings.owner_ids

    async def audit(handler, event, data):  # outer middleware: say once in the console who knocked
        user = getattr(event, "from_user", None)
        if user is not None and user.id not in owners and user.id not in app._denied_logged:
            app._denied_logged.add(user.id)
            log_warn(f"Бот проигнорировал пользователя {user.id} (@{user.username or '—'}): его нет в owner_ids")
        return await handler(event, data)

    router.message.outer_middleware(audit)
    router.callback_query.outer_middleware(audit)
    router.message.filter(F.from_user.id.in_(owners), F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.from_user.id.in_(owners))

    async def tidy(message: Message) -> None:
        """Drop the owner's message so the panel stays the last message of the chat (best effort)."""
        with contextlib.suppress(TelegramAPIError):
            await message.delete()

    def enqueue(links: list[tuple[str, str]]) -> tuple[int, int, int]:
        """(albums added, profiles added, already queued) - the queue is saved to disk before this returns."""
        albums = profiles = duplicates = 0
        for kind, url in links:
            if app.pipeline.submit(kind, url) is None:
                duplicates += 1
            elif kind == "account":
                profiles += 1
            else:
                albums += 1
        return albums, profiles, duplicates

    def describe_added(albums: int, profiles: int, duplicates: int) -> str:
        parts = []
        if albums:
            parts.append(count_text(albums, "альбом", "альбома", "альбомов"))
        if profiles:
            parts.append(count_text(profiles, "профиль", "профиля", "профилей"))
        text = ("➕ Добавлено: " + ", ".join(parts)) if parts else "Ничего нового не добавлено"
        if duplicates:
            text += f". Уже в очереди: {duplicates}"
        return text + "."

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        app.set_notice(HELP, 90)
        await tidy(message)
        await app.show_panel(message.chat.id, fresh=True)

    @router.message(Command("queue"))
    async def on_queue(message: Message) -> None:
        await tidy(message)
        await app.show_panel(message.chat.id, fresh=True)

    @router.message(Command("resend"))
    async def on_resend(message: Message) -> None:
        links = [item for item in extract_links(message) if item[0] == "album"]
        if not links:
            app.set_notice("Укажите ссылку на альбом: <code>/resend https://www.erome.com/a/…</code>")
        else:
            for _, url in links:
                app.state.reset_album(url)
            albums, _, duplicates = enqueue(links)
            app.set_notice("🔁 " + describe_added(albums, 0, duplicates))
            log(f"Повторная отправка: {len(links)}")
        await tidy(message)
        await app.show_panel(message.chat.id)

    @router.message(F.text | F.caption)
    async def on_text(message: Message) -> None:
        links = extract_links(message)
        if not links:
            app.set_notice("Не нашёл ссылок на альбом или профиль Erome. Пример: https://www.erome.com/a/AbCd1234")
        else:
            albums, profiles, duplicates = enqueue(links)  # already on disk
            app.set_notice(describe_added(albums, profiles, duplicates))
            if albums or profiles:
                log(f"Новые ссылки: альбомов {albums}, профилей {profiles}; в очереди {len(app.state.queued())}")
        await tidy(message)
        await app.show_panel(message.chat.id)

    @router.callback_query(F.data == "tg:pause")
    async def on_pause(query: CallbackQuery) -> None:
        app.control.paused = not app.control.paused
        log("Пауза включена из бота" if app.control.paused else "Продолжаю работу (из бота)")
        await query.answer("Пауза" if app.control.paused else "Продолжаю")
        await refresh_for(query)

    @router.callback_query(F.data == "tg:cancel")
    async def on_cancel(query: CallbackQuery) -> None:
        cancelled = app.pipeline.cancel_current()
        await query.answer("Отменяю текущий альбом…" if cancelled else "Сейчас ничего не идёт")
        await asyncio.sleep(0.3)
        await refresh_for(query)

    @router.callback_query(F.data == "tg:clear")
    async def on_clear(query: CallbackQuery) -> None:
        now = time.monotonic()
        if now < app.clear_confirm_until:
            removed = app.pipeline.clear_queue()
            app.clear_confirm_until = 0.0
            app.set_notice(f"🗑 Очередь очищена: {count_text(removed, 'ссылка', 'ссылки', 'ссылок')}")
            log_warn(f"Очередь очищена из бота: {removed}")
            await query.answer("Очередь очищена")
        else:
            app.clear_confirm_until = now + CONFIRM_SECONDS
            await query.answer("Нажмите ещё раз, чтобы очистить очередь")
        await refresh_for(query)

    async def refresh_for(query: CallbackQuery) -> None:
        if isinstance(query.message, Message):
            await app.show_panel(query.message.chat.id)

    return router


# ----- running -----------------------------------------------------------------------------------------


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
