"""The Telegram bot of the Telegram mode: takes Erome links from the owner and shows one live panel.

Links are queued durably before the owner's message is deleted, so nothing sent to the bot can get lost. The panel
is one message per owner that is edited in place; commands and buttons only change the queue and the pause flag.
Starting and stopping everything is tg_mode.py's business.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from erome.console import RunControl, log, log_warn
from erome.models import LinkKind
from erome.tg_pipeline import Pipeline, Progress
from erome.tg_settings import TelegramSettings
from erome.tg_state import CANCELLED, DONE, FAILED, PARTIAL, QUEUED, RUNNING, Job, TelegramState
from erome.tg_transport import STATE_READY
from erome.tg_userbot import Userbot
from erome.ui import human_duration, human_size, plural
from erome.urls import account_name_from_url, link_kind, normalize_download_url

LOG = logging.getLogger("erome.tg")

MSK = timezone(timedelta(hours=3), "МСК")
PANEL_EDIT_INTERVAL = 3.5  # seconds between edits of the live panel (Telegram allows about one per second)
PANEL_USER_EDIT_GAP = 1.2  # a reply to the owner is drawn at once unless the panel was edited this recently
NOTICE_SECONDS = 25
CONFIRM_SECONDS = 6
QUEUE_LINES = 5
RECENT_LINES = 5
PANEL_LIMIT = 3900
ANSWER_MERGE_SECONDS = 8.0  # links that follow an answer this closely are added to that same message
ANSWER_KEEP_SECONDS = 600.0  # an answer stays this long (or until the next one comes) and is then removed
ANSWER_MAX_LINES = 12

_LINK_RE = re.compile(r"(?<![\w.@/-])(?:https?://)?(?:www\.)?erome\.com/[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ").,;:!?]}>\"'"

HELP = (
    "Пришлите ссылку на <b>альбом</b> (erome.com/a/…) или на <b>профиль</b> (erome.com/имя) — "
    "можно сразу несколько: через пробел, с новой строки или отдельными сообщениями. "
    "Всё скачается и уйдёт в группу по очереди.\n"
    "Присылать можно в любой момент, даже пока что-то качается: ссылка встанет в очередь, а я сразу отвечу, "
    "что добавлено и что идёт сейчас.\n"
    "Профиль: прочитаю его сразу после текущего альбома и добавлю альбомы, которых ещё не отправляли, "
    "от старых к новым. Повторная ссылка на альбом ничего не продублирует — отправится только недостающее.\n"
    "Выложить альбом заново: <code>/resend ссылка</code>."
)


# ----- helpers -------------------------------------------------------------------------------------


def esc(text: object) -> str:
    """Text for Telegram's HTML mode (only &, < and > need escaping)."""
    return html.escape(str(text), quote=False)


def attr(text: object) -> str:
    return esc(text).replace('"', "&quot;")


def extract_links(message: Message) -> list[tuple[LinkKind, str]]:
    """(kind, url) of every Erome album/profile link in a message, in order, without duplicates."""
    text = message.text or message.caption or ""
    candidates: list[str] = []
    for entity in (message.entities or message.caption_entities or []):
        if entity.type == "text_link" and entity.url:
            candidates.append(entity.url)
    candidates.extend(match.group(0) for match in _LINK_RE.finditer(text))
    found: list[tuple[LinkKind, str]] = []
    seen: set[str] = set()
    for raw in candidates:
        raw = raw.strip().rstrip(_TRAILING)
        if not raw.lower().startswith("http"):
            raw = f"https://{raw}"
        url = normalize_download_url(raw)
        kind = link_kind(url)
        if kind in (LinkKind.ALBUM, LinkKind.ACCOUNT) and url not in seen:
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
    if job.kind == LinkKind.ACCOUNT:
        return link(job.title or f"Профиль {account_name_from_url(job.url)}", job.url)
    return link(job.title or job.url.replace("https://", ""), job.url)


def count_text(count: int, one: str, few: str, many: str) -> str:
    return f"{count} {plural(count, one, few, many)}"


def view_signature(text: str, markup: InlineKeyboardMarkup) -> tuple:
    return text, tuple(button.text for row in markup.inline_keyboard for button in row)


@dataclass
class Submitted:
    """What became of a link the owner sent: put in the queue now, or already there (or running)."""

    kind: LinkKind
    url: str
    job: Job
    added: bool
    repeat: bool = False  # sent by /resend


@dataclass
class Answer:
    """The bot's latest reply to the owner's links in a chat: kept to merge a burst into one message and to clean up."""

    message_id: int
    changed: float
    lines: list[str]
    status: bool


# ----- the application --------------------------------------------------------------------------------


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
        self._answers: dict[int, Answer] = {}
        self._answer_locks: dict[int, asyncio.Lock] = {}
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
        if progress.job.kind == LinkKind.ACCOUNT:
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

    def warning_lines(self) -> list[str]:
        """Why nothing may be moving right now (shown in the panel and at the end of an answer)."""
        warnings = []
        if self.userbot.state != STATE_READY:
            warnings.append(f"⚠ Telegram: {esc(self.userbot.panel_status())}")
        if self.pipeline.blocked:
            warnings.append(f"⚠ Нет доступа к группе или теме: {esc(self.pipeline.blocked)}")
        if self.pipeline.waiting and self.userbot.state == STATE_READY:
            warnings.append(f"⏳ {esc(self.pipeline.waiting)}")
        if self.paused:
            warnings.append("⏸ Пауза: текущая пачка файлов дойдёт до конца, дальше ничего не начнётся")
        return warnings

    def render(self) -> tuple[str, InlineKeyboardMarkup]:
        now = time.monotonic()
        lines = ["<b>Erome → Telegram</b>"]
        if self.notice and now < self.notice_until:
            lines += ["", self.notice]
        warnings = self.warning_lines()
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
                wait = job.not_before - time.time()
                again = f" — сайт не ответил, повтор через ≈{max(1, math.ceil(wait / 60))} мин" if wait > 0 else ""
                lines.append(f"{number}. {job_label(job)}{again}")
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
            message_id = self.state.panel_id(chat_id)
            if not fresh and message_id and time.monotonic() - self._last_edit.get(chat_id, 0.0) < PANEL_USER_EDIT_GAP:
                return  # edited a moment ago (a burst of links): the refresh loop draws the change right after
            text, markup = self.render()
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
                await self.expire_answers()
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

    # ----- answers to the owner's links -------------------------------------------------------------------

    def standing(self, job: Job) -> str:
        """Where a job stands, in words: running now, or its place in the queue."""
        if job.status == RUNNING:
            return "в работе прямо сейчас"
        before = self.state.jobs_before(job)
        if job.kind == LinkKind.ACCOUNT:
            when = "после «Продолжить»" if self.paused else "сразу после текущего" if before else "сразу"
            return f"в очереди (прочитаю {when})"
        text = f"в очереди №{1 + sum(1 for other in before if other.status == QUEUED)}"
        profiles = sum(1 for other in before if other.kind == LinkKind.ACCOUNT)
        if profiles:
            text += (
                f" (впереди {count_text(profiles, 'профиль', 'профиля', 'профилей')}: "
                f"{'его' if profiles == 1 else 'их'} альбомы встанут раньше)"
            )
        return text

    def submitted_line(self, item: Submitted) -> str:
        """One line of the answer about a link: added (and where), or why nothing was added."""
        label, where = job_label(item.job), self.standing(item.job)
        if not item.added:
            return f"↩ {label} — уже {where}"
        line = f"{'🔁' if item.repeat else '➕'} {label} — {where}"
        if item.kind == LinkKind.ALBUM and not item.repeat and self.state.is_album_done(item.url):
            line += ". Отправлялся раньше: проверю, всё ли дошло"
        return line

    def situation_lines(self) -> list[str]:
        """What is going on right now, for the end of an answer."""
        lines = self.warning_lines()
        progress = self.pipeline.progress
        if progress is not None:
            lines.append(f"<b>Сейчас:</b> {job_label(progress.job)} — {esc(self.activity_line(progress))}")
        elif not self.paused and self.state.queued():
            if self.state.next_queued() is not None:
                lines.append("Сейчас ничего не скачивается, начинаю сразу.")
            else:
                lines.append("Жду повторной попытки: сайт не ответил.")
        lines.append(f"В очереди: {len(self.state.queued())}")
        return lines

    def answer_text(self, body: list[str], status: bool) -> str:
        shown = body
        if len(body) > ANSWER_MAX_LINES:
            shown = body[: ANSWER_MAX_LINES - 1] + [f"… и ещё {len(body) - ANSWER_MAX_LINES + 1}"]
        parts = ["\n".join(shown)]
        if status:
            parts.append("\n".join(self.situation_lines()))
        return "\n\n".join(parts)

    def _answer_lock(self, chat_id: int) -> asyncio.Lock:
        return self._answer_locks.setdefault(chat_id, asyncio.Lock())

    async def answer(self, chat_id: int, lines: list[str], *, status: bool = True) -> None:
        """Reply to the owner at once, in a message of its own: what became of the links and what is going on now.

        Links that arrive in a burst share one message (it is edited); a later answer replaces the earlier one, and an
        old answer is removed after a while, so the chat does not fill up. If the reply cannot be sent, the same
        text goes to the panel's notice line.
        """
        if not lines:
            return
        try:
            async with self._answer_lock(chat_id):
                await self._answer(chat_id, lines, status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the links are queued already: a reply that failed must not hide that
            LOG.warning("Could not answer the owner: %s: %s", type(exc).__name__, exc)
            self.set_notice("\n".join(lines))

    async def _answer(self, chat_id: int, lines: list[str], status: bool) -> None:
        now = time.monotonic()
        previous = self._answers.get(chat_id)
        if previous is not None and now - previous.changed <= ANSWER_MERGE_SECONDS:
            merged = previous.lines + lines
            status = status or previous.status
            if await self._edit_answer(chat_id, previous.message_id, self.answer_text(merged, status)):
                self._answers[chat_id] = Answer(previous.message_id, now, merged, status)
                return
        elif previous is not None:
            await self._remove_answer(chat_id, previous.message_id)
        self._answers.pop(chat_id, None)  # nothing to add to: this answer starts a message of its own
        sent = await self.bot.send_message(chat_id, self.answer_text(lines, status))
        self._answers[chat_id] = Answer(sent.message_id, now, list(lines), status)

    async def _edit_answer(self, chat_id: int, message_id: int, text: str) -> bool:
        """Edit an earlier answer; False when it is gone."""
        try:
            await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                return True
            if "not found" in reason or "can't be edited" in reason or "message to edit" in reason:
                return False
            raise
        return True

    async def _remove_answer(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(TelegramAPIError):
            await self.bot.delete_message(chat_id, message_id)

    async def expire_answers(self) -> None:
        now = time.monotonic()
        for chat_id, answer in list(self._answers.items()):
            if now - answer.changed <= ANSWER_KEEP_SECONDS:
                continue
            async with self._answer_lock(chat_id):
                if self._answers.get(chat_id) is answer:  # not replaced while the lock was awaited
                    del self._answers[chat_id]
                    await self._remove_answer(chat_id, answer.message_id)

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
            if not queued:
                return "Жду ссылки в боте · очередь пуста"
            if self.state.next_queued() is None:
                return f"Жду повторной попытки: сайт не ответил{tail}"
            return f"Готовлюсь к следующему{tail}"
        title = progress.title or "…"
        line = f"«{title[:40]}» · {self.activity_line(progress)}"
        if self.pipeline.waiting:
            line += f" · {self.pipeline.waiting}"
        return line + tail


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

    def enqueue(links: list[tuple[LinkKind, str]], *, repeat: bool = False) -> list[Submitted]:
        """Put the links in the queue (it is saved to disk before this returns); tells what became of each one."""
        results: list[Submitted] = []
        for kind, url in links:
            job = app.pipeline.submit(kind, url)
            if job is not None:
                results.append(Submitted(kind, url, job, True, repeat))
            elif (known := app.state.find_open(url)) is not None:
                results.append(Submitted(kind, url, known, False, repeat))
        return results

    def log_added(items: list[Submitted]) -> None:
        albums = sum(1 for item in items if item.added and item.kind == LinkKind.ALBUM)
        profiles = sum(1 for item in items if item.added and item.kind == LinkKind.ACCOUNT)
        if albums or profiles:
            log(f"Новые ссылки: альбомов {albums}, профилей {profiles}; в очереди {len(app.state.queued())}")

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
        links = [item for item in extract_links(message) if item[0] == LinkKind.ALBUM]
        if not links:
            reply = app.answer(
                message.chat.id, ["Укажите ссылку на альбом: <code>/resend https://www.erome.com/a/…</code>"], status=False
            )
        else:
            # An album that is queued or being sent keeps its record: forgetting it mid-run would post its files twice.
            open_jobs = {url: app.state.find_open(url) for _, url in links}
            busy = [job for job in open_jobs.values() if job is not None]
            fresh = [(kind, url) for kind, url in links if open_jobs[url] is None]
            for _, url in fresh:
                app.state.reset_album(url)
            submitted = enqueue(fresh, repeat=True)
            if fresh:
                log(f"Повторная отправка: {len(fresh)}")
            lines = [app.submitted_line(item) for item in submitted]
            lines += [f"↩ {job_label(job)} — уже {app.standing(job)}: дождитесь окончания и повторите" for job in busy]
            reply = app.answer(message.chat.id, lines)
        await asyncio.gather(reply, tidy(message))
        await app.show_panel(message.chat.id)

    @router.message(F.text | F.caption)
    async def on_text(message: Message) -> None:
        links = extract_links(message)
        if not links:
            reply = app.answer(
                message.chat.id,
                ["Не нашёл ссылок на альбом или профиль Erome. Пример: https://www.erome.com/a/AbCd1234"],
                status=False,
            )
        else:
            submitted = enqueue(links)  # on disk before anything is answered
            log_added(submitted)
            reply = app.answer(message.chat.id, [app.submitted_line(item) for item in submitted])
        await asyncio.gather(reply, tidy(message))
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
