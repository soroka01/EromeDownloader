"""Telegram userbot: one long-lived Telethon client that posts the downloaded media to the target topic.

The connection handling (supervisor, deadlines, extra upload connections, QR sign-in) is the one proven in the
Webcam and Bigo recorders. The sending side is specific to this project: photos and videos go out as albums
(media groups of up to 10), anything else as single messages. Every message uses a deterministic random_id, so
repeating a batch after a lost answer or a restart can never post a file twice.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import getpass
import io
import logging
import os
import random
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from telethon import TelegramClient, errors, functions, types, utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER

from erome.tg_media import SendItem
from erome.tg_settings import TelegramSettings
from erome.ui import human_size

LOG = logging.getLogger("erome.tg")

STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_OFFLINE = "offline"
STATE_UNAUTHORIZED = "unauthorized"
STATE_ERROR = "error"

# Telegram: 2000 MiB for regular accounts, 4000 MiB for Premium. Slightly lower decimal values keep a margin.
FREE_UPLOAD_LIMIT_BYTES = 2_000_000_000
PREMIUM_UPLOAD_LIMIT_BYTES = 4_000_000_000
ALBUM_MAX_ITEMS = 10

# A connection that Telegram's side (or something in between) silently stops answering must not freeze the
# userbot forever: every network step has a deadline, and a stalled connection is dropped and rebuilt.
CONNECT_TIMEOUT_SECONDS = 30.0
HANDSHAKE_TIMEOUT_SECONDS = 30.0
RESOLVE_TIMEOUT_SECONDS = 180.0  # a fresh session may have to walk many dialogs to find the group
REQUEST_TIMEOUT_SECONDS = 90.0
SEND_TIMEOUT_SECONDS = 120.0
UPLOAD_STALL_SECONDS = 150.0  # no new file part for this long = the connection is dead
UPLOAD_WATCH_INTERVAL_SECONDS = 5.0
# An uploaded file stays usable on Telegram's side for a while; a repeated attempt (flood wait, closed topic, a lost
# answer) reuses the handle instead of sending the whole file again.
UPLOAD_REUSE_SECONDS = 1800.0

# Parts of a big file are spread over MANY connections to the same DC, a few parts each (one connection carries
# only ~1-3 MB/s; 8+ parts in flight on one connection is slower than 3). Measured on a Premium account:
#   6 connections x 8 parts 7.5 MB/s | 16 x 3 10.0 | 24 x 3 15.8 | 32+ broke: corrupted packets.
DEFAULT_PARALLEL_PARTS = 3
EXTRA_CONNECTION_RETRY_SECONDS = 180.0
# Telegram answers too many connections with a transport-level flood ("HTTP code 429" in the stream) and then drops
# every connection at once. After this many lost extra connections within the window the pool is halved.
LANE_FAILURE_LIMIT = 3
LANE_FAILURE_WINDOW_SECONDS = 120.0
MIN_UPLOAD_CONNECTIONS = 4
# The pool is found by probing: it starts at START, grows by STEP after each quiet period and is halved when
# Telegram drops connections.
UPLOAD_CONNECTIONS_START = 8
UPLOAD_CONNECTIONS_STEP = 2
UPLOAD_CONNECTIONS_GROW_AFTER_SECONDS = 300.0
# Telegram counts requests, not bytes: 512 KB parts (the maximum) halve the request rate of 256 KB ones.
UPLOAD_PART_SIZE_BYTES = 512 * 1024
BIG_FILE_THRESHOLD_BYTES = 10 * 1024 * 1024
PARALLEL_UPLOAD_MAX_BYTES = 2_000_000_000
SMALL_UPLOAD_CONCURRENCY = 4  # files of up to 10 MB are uploaded this many at a time


class UserbotUnavailable(RuntimeError):
    """Telegram is not connected (yet); nothing was sent."""


class RetryLater(RuntimeError):
    """Telegram asked to wait (flood control); nothing was sent and it is not a failed attempt."""

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason or f"retry in {seconds:.0f}s")
        self.seconds = float(seconds)


class DeliveryUncertain(RuntimeError):
    """The send request may have reached Telegram; repeating it is safe (same random_id)."""


class DestinationError(RuntimeError):
    """The target chat/topic refuses our messages (no rights, closed topic, ...): waiting may fix it, retrying will not."""


class GroupRejected(RuntimeError):
    """Telegram refused the whole album; nothing was posted and the files can still go out one by one."""


class WrongAccount(ValueError):
    """The session belongs to another account than the configured one."""


SESSION_ERRORS = (
    errors.AuthKeyDuplicatedError,
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)


def _error_classes(*names: str) -> tuple[type[BaseException], ...]:
    return tuple(getattr(errors, name) for name in names if hasattr(errors, name))


DESTINATION_ERRORS = _error_classes(
    "ChatWriteForbiddenError", "ChatAdminRequiredError", "UserBannedInChannelError", "ChannelPrivateError",
    "ChannelInvalidError", "PeerIdInvalidError", "TopicDeletedError", "ChatRestrictedError",
    "ChatSendMediaForbiddenError", "ChatSendPhotosForbiddenError", "ChatSendVideosForbiddenError",
    "ChatSendDocsForbiddenError", "ChatSendGifsForbiddenError", "ChatSendPlainForbiddenError",
)
PHOTO_ERRORS = _error_classes(
    "PhotoInvalidDimensionsError", "PhotoSaveFileInvalidError", "ImageProcessFailedError", "PhotoExtInvalidError",
    "PhotoCropSizeSmallError", "PhotoInvalidError", "MediaInvalidError",
)
# A kept upload handle that Telegram no longer knows (expired, other DC, parts lost): upload the file again.
STALE_UPLOAD_ERRORS = _error_classes(
    "FilePartMissingError", "FilePartsInvalidError", "FileIdInvalidError", "FilePartInvalidError",
    "FilePart0MissingError", "FilePartLengthInvalidError", "FilePartSizeInvalidError", "FilePartSizeChangedError",
)
# Telegram's own hiccups (HTTP 500 family). Telethon retries them five times and, with raise_last_call_error, raises
# the last one: RANDOM_ID_DUPLICATE is one of them and means "an earlier attempt of this message went through".
TRANSIENT_RPC_ERRORS = _error_classes(
    "ServerError", "RpcCallFailError", "RpcMcgetFailError", "InterdcCallErrorError", "InterdcCallRichErrorError",
    "TimedOutError",
)
# Trouble with the link, not with the data: the same request can simply be repeated later.
TRANSIENT_ERRORS = (ConnectionError, asyncio.TimeoutError, DeliveryUncertain, UserbotUnavailable, *TRANSIENT_RPC_ERRORS)


def is_destination_error(exc: BaseException) -> bool:
    return isinstance(exc, DESTINATION_ERRORS) or "TOPIC_CLOSED" in str(getattr(exc, "message", "") or exc)


def describe_error(exc: BaseException) -> str:
    message = str(getattr(exc, "message", "") or exc)
    return message[:160] or type(exc).__name__


@dataclass
class UploadHandle:
    """Live state of one file upload; the stall watchdog and the progress meter read it."""

    name: str
    total: int
    sent: int = 0
    listener: Callable[[int], None] | None = None
    started_at: float = field(default_factory=time.monotonic)

    def advance(self, count: int) -> None:
        self.sent += count
        if self.listener is not None and count > 0:
            self.listener(count)

    def set(self, sent: int) -> None:
        delta = sent - self.sent
        self.sent = sent
        if self.listener is not None and delta > 0:
            self.listener(delta)


def cryptg_available() -> bool:
    try:
        import cryptg  # noqa: F401
    except ImportError:
        return False
    return True


class _QuietTelegramClient(TelegramClient):
    """A client that never listens for updates.

    Telethon's own update loop keeps polling `getDifference` even for a client that asked for no updates. After a
    network outage it can crash on a half-finished difference and disconnect the client, which cancels every
    request in flight (uploads included). This program only sends, so the loop is replaced by an idle one.
    """

    async def _update_loop(self) -> None:
        queue = getattr(self, "_updates_queue", None)
        while self.is_connected():
            if queue is None:
                await asyncio.sleep(30)
                continue
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), 30)  # whatever arrives unasked is dropped

    async def _keepalive_loop(self) -> None:
        """Telethon's loop waits on `self.disconnected` with a 60 s timeout. After a transport-level flood the client
        can still count as connected while that future is already finished: `wait_for` then returns at once, the
        loop never yields and the whole event loop starves (no log lines, uploads stuck, memory growing). This
        version always sleeps first.
        """
        while self.is_connected():
            await asyncio.sleep(60)
            try:
                await self._clean_exported_senders()
                if not self._sender._transport_connected():
                    continue
                self._sender._keepalive_ping(random.randrange(-(2**63), 2**63))
                await self._save_states_and_entities()
                await utils.maybe_async(self.session.save())
            except ConnectionError:
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed housekeeping step must not stop the loop
                LOG.debug("Telegram keepalive step failed: %s", exc)


def task_is_cancelling() -> bool:
    """True when this very task was asked to stop (as opposed to something it awaits being cancelled)."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def new_client(
    settings: TelegramSettings,
    *,
    connection_retries: int = 3,
    receive_updates: bool = False,
) -> TelegramClient:
    """receive_updates must stay on for the QR sign-in (the login token arrives as an update)."""
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    factory = TelegramClient if receive_updates else _QuietTelegramClient
    return factory(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        connection_retries=connection_retries,
        request_retries=5,
        retry_delay=2,
        auto_reconnect=True,
        receive_updates=receive_updates,
        # Without this a RANDOM_ID_DUPLICATE (or any other 500) that survives Telethon's five retries turns into a
        # bare ValueError, and the "already delivered" answer is never seen.
        raise_last_call_error=True,
    )


INVISIBLE_CHARS = dict.fromkeys(
    [0x115F, 0x1160, 0x3164, 0xFFA0, 0x2060, 0xFEFF, *range(0x200B, 0x2010)], None
)
MAX_NAME_LENGTH = 40
# What the legacy Windows console can draw with a predictable cell width: Latin, Greek, Cyrillic, common punctuation.
RENDERABLE_RANGES = ((0x20, 0x24F), (0x370, 0x52F), (0x1E00, 0x1EFF), (0x2010, 0x2027), (0x2030, 0x205E), (0x20A0, 0x20BF))


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(INVISIBLE_CHARS)  # fancy letters become plain ones
    text = " ".join(text.split())
    text = "".join(ch for ch in text if any(low <= ord(ch) <= high for low, high in RENDERABLE_RANGES))
    text = " ".join(text.split())
    return text if len(text) <= MAX_NAME_LENGTH else text[: MAX_NAME_LENGTH - 1] + "…"


def display_name(entity: Any) -> str:
    name = clean_text(
        " ".join(
            str(part)
            for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
            if part
        )
    )
    username = getattr(entity, "username", None)
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else str(getattr(entity, "id", "unknown")))


class _Unauthorized(Exception):
    pass


def _random_id() -> int:
    value = int.from_bytes(os.urandom(8), "big", signed=True)
    return value or 1


class Userbot:
    def __init__(self, settings: TelegramSettings) -> None:
        self.settings = settings
        self.chat_id = settings.chat_id
        self.thread_id = settings.thread_id
        self.state = STATE_STARTING
        self.error = ""
        self.account_id: int | None = None
        self.account_name = ""
        self.chat_title = ""
        self.topic_title = ""
        self.premium = False
        self.flood_until = 0.0
        self.parallel_parts = DEFAULT_PARALLEL_PARTS
        self.upload_connections = settings.upload_connections
        self._extra_senders: list[MTProtoSender] = []
        self._extra_owner: TelegramClient | None = None
        self._extra_lock: asyncio.Lock | None = None
        self._extra_retry_at = 0.0
        self._lane_failures: list[float] = []
        self._lane_target = 0  # current size of the pool (0 = not chosen yet)
        self._lane_quiet_since = time.monotonic()
        self.client: TelegramClient | None = None
        self._peer: Any = None
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._supervise(), name="userbot")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._ready.clear()
        await self._close_extra_senders()
        if self.client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.client.disconnect(), 10)

    @property
    def ready(self) -> bool:
        return self.state == STATE_READY and self.client is not None and self.client.is_connected()

    async def wait_ready(self, timeout: float | None = None) -> bool:
        if self.ready:
            return True
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self.ready

    @property
    def max_upload_bytes(self) -> int:
        return PREMIUM_UPLOAD_LIMIT_BYTES if self.premium else FREE_UPLOAD_LIMIT_BYTES

    def status_text(self) -> str:
        """Short human description of the connection for the console and the bot panel."""
        if self.state == STATE_READY:
            return "подключён"
        if self.state == STATE_UNAUTHORIZED:
            return "сессия не авторизована или отозвана — запустите режим заново и отсканируйте QR"
        if self.state == STATE_ERROR:
            return self.error or "ошибка настроек"
        if self.state == STATE_OFFLINE:
            return "нет связи, переподключаюсь"
        return "подключаюсь"

    def _set_state(self, state: str, error: str = "") -> None:
        if state != self.state or error != self.error:
            LOG.info("Telegram userbot state: %s%s", state, f" ({error})" if error else "")
        self.state = state
        self.error = error
        if state != STATE_READY:
            self._ready.clear()

    async def _supervise(self) -> None:
        delay = 3.0
        while not self._stopping:
            try:
                if self.state == STATE_READY and self.client is not None and self.client.is_connected():
                    await asyncio.sleep(5)
                    continue
                if self.state == STATE_READY:
                    self._set_state(STATE_OFFLINE, "connection lost")
                await self._connect_once()
                delay = 3.0
            except asyncio.CancelledError:
                if self._stopping or task_is_cancelling():
                    raise
                # Telethon cancelled a request this loop was awaiting (the connection went away): not a shutdown.
                self._set_state(STATE_OFFLINE, "connection interrupted")
                await asyncio.sleep(delay)
            except SESSION_ERRORS as exc:
                self._set_state(STATE_UNAUTHORIZED, f"{type(exc).__name__}: session is no longer valid")
                await asyncio.sleep(30)
            except _Unauthorized:
                self._set_state(STATE_UNAUTHORIZED, "session is not authorized: restart the mode and sign in")
                await asyncio.sleep(30)
            except ValueError as exc:
                self._set_state(STATE_ERROR, str(exc))
                await asyncio.sleep(60)
            except Exception as exc:
                self._set_state(STATE_OFFLINE, f"{type(exc).__name__}: {exc}"[:200])
                LOG.warning("Telegram userbot connection failed: %s", exc)
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)

    async def _connect_once(self) -> None:
        if self.client is None:
            self.client = new_client(self.settings)
        client = self.client
        step = "connect"
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_SECONDS)
            step = "authorization"
            me = await asyncio.wait_for(self._authorize(client), HANDSHAKE_TIMEOUT_SECONDS)
            expected = self.settings.account_id
            if expected and int(me.id) != expected:
                raise WrongAccount(
                    f"сессия принадлежит другому аккаунту ({display_name(me)}), ожидался ID {expected}: "
                    "удалите data/telegram.session и войдите заново"
                )
            self.account_id = int(me.id)
            self.account_name = display_name(me)
            self.premium = bool(getattr(me, "premium", False))
            step = "group lookup"
            await asyncio.wait_for(self._resolve_peer(client), RESOLVE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            message = f"Telegram does not answer ({step})"
            self._drop_connection(message)
            raise ConnectionError(message) from None
        self._set_state(STATE_READY)
        self._ready.set()

    @staticmethod
    async def _authorize(client: TelegramClient) -> Any:
        if not await client.is_user_authorized():
            raise _Unauthorized
        return await client.get_me()

    def _drop_connection(self, reason: str) -> None:
        """Forget a connection that stopped answering: the supervisor builds a fresh client."""
        client, self.client = self.client, None
        self._set_state(STATE_OFFLINE, reason)
        if client is not None:
            asyncio.ensure_future(self._safe_disconnect(client))
        asyncio.ensure_future(self._close_extra_senders())

    @staticmethod
    async def _safe_disconnect(client: TelegramClient) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.disconnect(), 10)

    async def _resolve_peer(self, client: TelegramClient) -> None:
        try:
            peer = await client.get_input_entity(self.chat_id)
        except ValueError:
            # A fresh session knows no entities yet: walk the dialogs until the group shows up.
            async for dialog in client.iter_dialogs():
                if dialog.id == self.chat_id:
                    break
            try:
                peer = await client.get_input_entity(self.chat_id)
            except ValueError:
                raise ValueError(f"группа {self.chat_id} не найдена среди чатов этого аккаунта") from None
        entity = await client.get_entity(peer)
        if utils.get_peer_id(entity) != self.chat_id:
            raise ValueError("найденная группа не совпадает с chat_id из telegram.json")
        self._peer = peer
        self.chat_title = str(getattr(entity, "title", "") or "")
        self.topic_title = ""
        if self.thread_id and isinstance(entity, types.Channel) and getattr(entity, "forum", False):
            await self._check_topic(client)

    async def _check_topic(self, client: TelegramClient) -> None:
        try:
            result = await asyncio.wait_for(
                client(functions.messages.GetForumTopicsByIDRequest(peer=self._peer, topics=[self.thread_id])),
                REQUEST_TIMEOUT_SECONDS,
            )
        except errors.RPCError as exc:
            LOG.warning("Could not check topic %s: %s", self.thread_id, describe_error(exc))
            return
        topic = next(iter(getattr(result, "topics", []) or []), None)
        if isinstance(topic, types.ForumTopicDeleted):
            raise ValueError(f"тема {self.thread_id} удалена из группы")
        if isinstance(topic, types.ForumTopic):
            self.topic_title = str(topic.title or "")
            if getattr(topic, "closed", False):
                LOG.warning("Topic %s is closed: only admins can post there", self.thread_id)

    # --- requests ----------------------------------------------------------
    def _require_ready(self) -> TelegramClient:
        if not self.ready or self.client is None:
            raise UserbotUnavailable(f"Telegram is not connected ({self.state}{': ' + self.error if self.error else ''})")
        return self.client

    @contextlib.contextmanager
    def _session_guard(self):
        """A session revoked while connected means nothing was sent: report it as unavailable, not as a failure.

        When the connection drops Telethon cancels the requests in flight; the caller's task is not being cancelled
        then, so it must see an ordinary connection error and not a CancelledError that would end the whole task.
        """
        try:
            yield
        except asyncio.CancelledError:
            if task_is_cancelling():
                raise
            raise ConnectionError("Telegram connection was lost") from None
        except SESSION_ERRORS as exc:
            self._set_state(STATE_UNAUTHORIZED, f"{type(exc).__name__}: session is no longer valid")
            raise UserbotUnavailable(f"Telegram session is no longer valid ({type(exc).__name__})") from exc

    async def _call(self, request: Any, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Any:
        """One RPC. Telethon sleeps through flood waits of up to a minute by itself; a longer one is reported as
        RetryLater, so the caller can show it, stay responsive to a stop request and repeat the step afterwards."""
        client = self._require_ready()
        if (pause := self.flood_until - time.monotonic()) > 0:
            raise RetryLater(pause, "Telegram flood wait")  # one flood wait holds back every request, not one
        try:
            with self._session_guard():
                return await asyncio.wait_for(client(request), timeout)
        except asyncio.TimeoutError:
            if client is self.client:
                self._drop_connection(f"Telegram did not answer {type(request).__name__} in {timeout:.0f}s")
            raise
        except errors.FloodError as exc:
            seconds = int(getattr(exc, "seconds", 0) or 30)
            self.flood_until = max(self.flood_until, time.monotonic() + seconds + 1)
            LOG.warning("Telegram flood wait of %s s on %s", seconds, type(request).__name__)
            raise RetryLater(seconds + 1, f"Telegram flood wait {seconds}s") from exc

    # --- sending -----------------------------------------------------------
    def _reply_to(self) -> Any:
        return types.InputReplyToMessage(reply_to_msg_id=int(self.thread_id)) if self.thread_id else None

    @staticmethod
    def _units(items: list[SendItem]) -> list[list[SendItem]]:
        """Consecutive photos/videos form albums of up to 10; anything else is a message of its own."""
        units: list[list[SendItem]] = []
        album: list[SendItem] = []
        for item in items:
            if item.kind in ("photo", "video"):
                album.append(item)
                if len(album) == ALBUM_MAX_ITEMS:
                    units.append(album)
                    album = []
                continue
            if album:
                units.append(album)
                album = []
            units.append([item])
        if album:
            units.append(album)
        return units

    async def send_batch(
        self,
        items: list[SendItem],
        caption: str,
        *,
        on_delivered: Callable[[list[SendItem]], None],
        on_bytes: Callable[[int], None] | None = None,
    ) -> dict[str, str]:
        """Post the files to the target topic, in order.

        Returns {file key: reason} for the files Telegram refused for good. Delivered files are reported through
        `on_delivered` the moment Telegram accepts them, so a repeated call only needs to carry the rest.
        Raises UserbotUnavailable, RetryLater, DestinationError or the underlying network error; none of these
        means that anything was posted twice, because every message keeps its random_id. Uploaded file handles
        stay on the items, so a repeated call does not send the bytes again.
        """
        refused: dict[str, str] = {}
        sendable: list[SendItem] = []
        for item in items:
            if item.size <= 0:
                refused[item.key] = "пустой файл"
            elif item.size > self.max_upload_bytes:
                refused[item.key] = f"файл больше лимита Telegram ({human_size(self.max_upload_bytes)})"
            else:
                sendable.append(item)
        if not sendable:
            return refused
        client = self._require_ready()
        upload_refused = await self._upload_items(client, sendable, on_bytes)
        refused.update(upload_refused)  # a file Telegram cannot take must not hold back the others
        sendable = [item for item in sendable if item.key not in upload_refused]
        caption_left = caption
        for unit in self._units(sendable):
            unit_refused, delivered_any = await self._send_unit(unit, caption_left, on_delivered)
            refused.update(unit_refused)
            if delivered_any:
                caption_left = ""  # the title goes out once, with the first message that really arrived
        return refused

    async def _upload_items(
        self, client: TelegramClient, items: list[SendItem], on_bytes: Callable[[int], None] | None
    ) -> dict[str, str]:
        """Upload what is not on Telegram's side yet. Returns {key: reason} for files Telegram refused to take."""
        rejected: dict[str, str] = {}
        semaphore = asyncio.Semaphore(SMALL_UPLOAD_CONCURRENCY)

        async def upload(item: SendItem) -> None:
            if item.file is not None and time.monotonic() - item.uploaded_at < UPLOAD_REUSE_SECONDS:
                return
            item.file = item.thumb_file = None
            handle = UploadHandle(name=item.path.name, total=item.size, listener=on_bytes)
            try:
                file = await self._upload_with_watchdog(client, item.path, handle)
            except (errors.FloodError, *SESSION_ERRORS, *TRANSIENT_RPC_ERRORS):
                raise
            except errors.RPCError as exc:  # this very file is refused: the others still go out
                rejected[item.key] = describe_error(exc)
                return
            thumb = None
            if item.thumb is not None:
                try:
                    thumb = await asyncio.wait_for(client.upload_file(item.thumb), REQUEST_TIMEOUT_SECONDS)
                except (errors.FloodError, *SESSION_ERRORS, *TRANSIENT_RPC_ERRORS):
                    raise
                except errors.RPCError as exc:
                    LOG.info("Thumbnail of %s was not accepted (%s); sending without it", item.path.name, describe_error(exc))
            item.file, item.thumb_file, item.uploaded_at = file, thumb, time.monotonic()

        async def upload_small(item: SendItem) -> None:
            async with semaphore:
                await upload(item)

        small = [item for item in items if item.size <= BIG_FILE_THRESHOLD_BYTES]
        big = [item for item in items if item.size > BIG_FILE_THRESHOLD_BYTES]
        try:
            with self._session_guard():
                tasks = [asyncio.ensure_future(upload_small(item)) for item in small]
                try:
                    if tasks:
                        await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                for item in big:
                    await upload(item)
        except errors.FloodError as exc:
            seconds = int(getattr(exc, "seconds", 0) or 30)
            self.flood_until = max(self.flood_until, time.monotonic() + seconds + 1)
            raise RetryLater(seconds + 1, f"Telegram flood wait {seconds}s") from exc
        return rejected

    @staticmethod
    def _forget_upload(item: SendItem) -> bool:
        """Drop a handle Telegram no longer accepts. False once it happened twice: then the file itself is bad."""
        item.stale_resets += 1
        item.file = item.thumb_file = None
        item.uploaded_at = 0.0
        return item.stale_resets <= 2

    def _uploaded_media(self, item: SendItem) -> Any:
        if item.kind == "photo":
            return types.InputMediaUploadedPhoto(file=item.file)
        attributes: list[Any] = []
        if item.kind == "video":
            video = item.video
            attributes.append(
                types.DocumentAttributeVideo(
                    duration=float(video.duration) if video else 0.0,
                    w=int(video.width) if video else 0,
                    h=int(video.height) if video else 0,
                    supports_streaming=True,
                )
            )
        attributes.append(types.DocumentAttributeFilename(file_name=item.path.name))
        animated = item.kind == "file" and item.path.suffix.lower() == ".gif"
        if animated:
            attributes.append(types.DocumentAttributeAnimated())
        return types.InputMediaUploadedDocument(
            file=item.file,
            mime_type=item.mime,
            attributes=attributes,
            thumb=item.thumb_file,
            force_file=True if item.kind == "file" and not animated else None,
        )

    async def _send_unit(
        self, unit: list[SendItem], caption: str, on_delivered: Callable[[list[SendItem]], None]
    ) -> tuple[dict[str, str], bool]:
        """Post one album or one single file. Returns ({key: reason} of refused files, whether anything arrived).

        Every message that Telegram accepted is reported at once - not after the whole unit - so a failure later in
        the unit never makes the caller send the earlier files again.
        """
        refused: dict[str, str] = {}
        if len(unit) > 1:
            try:
                await self._send_album(unit, caption)
            except GroupRejected as exc:
                LOG.info("Telegram refused the album (%s); sending its %d files one by one", exc, len(unit))
            else:
                on_delivered(list(unit))
                return refused, True
        delivered_any = False
        for item in unit:
            reason = await self._send_single(item, "" if delivered_any else caption)
            if reason is None:
                on_delivered([item])
                delivered_any = True
            else:
                refused[item.key] = reason
        return refused, delivered_any

    async def _send_single(self, item: SendItem, caption: str) -> str | None:
        """Post one file. None = delivered; a string = the reason Telegram refused it for good."""
        request = functions.messages.SendMediaRequest(
            peer=self._peer,
            media=self._uploaded_media(item),
            message=caption,
            random_id=item.random_id,
            reply_to=self._reply_to(),
        )
        try:
            await self._call(request, timeout=SEND_TIMEOUT_SECONDS)
        except errors.RandomIdDuplicateError:
            return None  # an earlier attempt of this very message went through
        except TRANSIENT_RPC_ERRORS as exc:
            raise DeliveryUncertain(f"{type(exc).__name__}: {exc}") from exc
        except STALE_UPLOAD_ERRORS as exc:
            if self._forget_upload(item):
                raise DeliveryUncertain(f"stale upload of {item.path.name}: {describe_error(exc)}") from exc
            return describe_error(exc)
        except errors.RPCError as exc:
            if is_destination_error(exc):
                raise DestinationError(describe_error(exc)) from exc
            if item.kind == "photo" and isinstance(exc, PHOTO_ERRORS):
                LOG.info("Telegram refused %s as a photo (%s); sending it as a file", item.path.name, describe_error(exc))
                item.kind = "file"
                return await self._send_single(item, caption)
            return describe_error(exc)
        except (asyncio.CancelledError, UserbotUnavailable, RetryLater):
            raise
        except Exception as exc:
            raise DeliveryUncertain(f"{type(exc).__name__}: {exc}") from exc
        return None

    async def _send_album(self, unit: list[SendItem], caption: str) -> None:
        media_items: list[types.InputSingleMedia] = []
        for index, item in enumerate(unit):
            try:
                registered = await self._call(
                    functions.messages.UploadMediaRequest(peer=self._peer, media=self._uploaded_media(item))
                )
            except TRANSIENT_RPC_ERRORS as exc:
                raise DeliveryUncertain(f"{type(exc).__name__}: {exc}") from exc
            except STALE_UPLOAD_ERRORS as exc:
                if self._forget_upload(item):
                    raise DeliveryUncertain(f"stale upload of {item.path.name}: {describe_error(exc)}") from exc
                raise GroupRejected(describe_error(exc)) from exc
            except errors.RPCError as exc:
                if is_destination_error(exc):
                    raise DestinationError(describe_error(exc)) from exc
                if item.kind == "photo" and isinstance(exc, PHOTO_ERRORS):
                    item.kind = "file"  # a document cannot join an album: the files go out one by one
                raise GroupRejected(describe_error(exc)) from exc
            media = getattr(registered, "photo", None) or getattr(registered, "document", None)
            if media is None:
                raise GroupRejected("Telegram did not return the uploaded media")
            media_items.append(
                types.InputSingleMedia(
                    media=utils.get_input_media(media),
                    message=caption if index == 0 else "",
                    random_id=item.random_id,
                )
            )
        request = functions.messages.SendMultiMediaRequest(
            peer=self._peer, multi_media=media_items, reply_to=self._reply_to()
        )
        try:
            await self._call(request, timeout=SEND_TIMEOUT_SECONDS)
        except errors.RandomIdDuplicateError:
            return  # an earlier attempt of this very album went through
        except TRANSIENT_RPC_ERRORS as exc:
            raise DeliveryUncertain(f"{type(exc).__name__}: {exc}") from exc
        except STALE_UPLOAD_ERRORS as exc:
            if all([self._forget_upload(item) for item in unit]):
                raise DeliveryUncertain(f"stale upload in the album: {describe_error(exc)}") from exc
            raise GroupRejected(describe_error(exc)) from exc
        except errors.RPCError as exc:
            if is_destination_error(exc):
                raise DestinationError(describe_error(exc)) from exc
            raise GroupRejected(describe_error(exc)) from exc
        except (asyncio.CancelledError, UserbotUnavailable, RetryLater):
            raise
        except Exception as exc:
            raise DeliveryUncertain(f"{type(exc).__name__}: {exc}") from exc

    # --- upload connections -------------------------------------------------
    async def _upload_file(self, client: TelegramClient, path: Any, handle: UploadHandle) -> Any:
        size = handle.total
        if self.parallel_parts > 1 and BIG_FILE_THRESHOLD_BYTES < size <= PARALLEL_UPLOAD_MAX_BYTES:
            return await self._upload_big_parallel(client, path, size, handle)
        return await client.upload_file(
            path, file_name=path.name, progress_callback=lambda sent, _total: handle.set(sent)
        )

    async def _new_upload_sender(self, client: TelegramClient) -> MTProtoSender:
        """One more connection to the client's own DC with the same auth key (parts of one file are spread over them)."""
        dc = await client._get_dc(client.session.dc_id)
        # No automatic reconnection: a lane that breaks is dropped (its parts go over the main connection); two
        # dozen senders redialling at once is what turns a hiccup into a flood.
        sender = MTProtoSender(client.session.auth_key, loggers=client._log, auto_reconnect=False, retries=1)
        try:
            await sender.connect(
                client._connection(
                    dc.ip_address, dc.port, dc.id, loggers=client._log, proxy=client._proxy, local_addr=client._local_addr
                )
            )
            init = copy.copy(client._init_request)
            init.query = functions.help.GetConfigRequest()
            await sender.send(functions.InvokeWithLayerRequest(LAYER, init))
        except BaseException:
            with contextlib.suppress(Exception):
                await sender.disconnect()
            raise
        return sender

    async def _extra_upload_senders(self, client: TelegramClient) -> list[MTProtoSender]:
        """Extra upload connections, created on demand and reused. Failing to get them just means fewer lanes."""
        wanted = self._effective_upload_connections() - 1
        if wanted <= 0:
            return []
        if self._extra_lock is None:
            self._extra_lock = asyncio.Lock()
        async with self._extra_lock:
            if self._extra_owner is not client:
                await self._close_extra_senders()
                self._extra_owner = client
            while len(self._extra_senders) > wanted:  # the pool was halved: let the surplus go
                surplus = self._extra_senders.pop()
                asyncio.ensure_future(self._safe_disconnect_sender(surplus))
            missing = wanted - len(self._extra_senders)
            if missing > 0 and time.monotonic() >= self._extra_retry_at:
                # Opened side by side: one after another, two dozen handshakes would delay the first file by a minute.
                openings = [
                    asyncio.ensure_future(
                        asyncio.wait_for(
                            self._new_upload_sender(client), CONNECT_TIMEOUT_SECONDS + HANDSHAKE_TIMEOUT_SECONDS
                        )
                    )
                    for _ in range(missing)
                ]
                try:
                    results = await asyncio.gather(*openings, return_exceptions=True)
                except BaseException:  # cancelled while dialling: close what had already connected
                    for opening in openings:
                        opening.cancel()
                    for late in await asyncio.gather(*openings, return_exceptions=True):
                        if isinstance(late, MTProtoSender):
                            asyncio.ensure_future(self._safe_disconnect_sender(late))
                    raise
                failures = 0
                for result in results:
                    if isinstance(result, MTProtoSender):
                        self._extra_senders.append(result)
                    elif isinstance(result, asyncio.CancelledError) and task_is_cancelling():
                        raise result
                    else:
                        failures += 1
                if failures:
                    LOG.warning(
                        "%d of %d extra upload connections failed to open (%s); uploading over fewer connections",
                        failures, missing, next((repr(r) for r in results if isinstance(r, BaseException)), ""),
                    )
                    self._extra_retry_at = time.monotonic() + EXTRA_CONNECTION_RETRY_SECONDS
                LOG.info("Telegram upload connections: %d", 1 + len(self._extra_senders))
            return list(self._extra_senders)

    def _effective_upload_connections(self) -> int:
        """How many connections to use now: probed upwards from a safe start, halved after losses."""
        cap = max(1, self.upload_connections)
        now = time.monotonic()
        if self._lane_target <= 0:
            self._lane_target = min(cap, UPLOAD_CONNECTIONS_START)
            self._lane_quiet_since = now
        elif self._lane_target < cap and now - self._lane_quiet_since >= UPLOAD_CONNECTIONS_GROW_AFTER_SECONDS:
            self._lane_target = min(cap, self._lane_target + UPLOAD_CONNECTIONS_STEP)
            self._lane_quiet_since = now
            LOG.info("Telegram upload connections raised to %d (cap %d)", self._lane_target, cap)
        self._lane_target = min(self._lane_target, cap)
        return self._lane_target

    def _note_lane_failure(self) -> None:
        now = time.monotonic()
        self._lane_failures.append(now)
        del self._lane_failures[:-16]
        self._lane_quiet_since = now
        recent = sum(1 for stamp in self._lane_failures if now - stamp <= LANE_FAILURE_WINDOW_SECONDS)
        if recent >= LANE_FAILURE_LIMIT:
            before = self._lane_target or self.upload_connections
            self._lane_target = max(MIN_UPLOAD_CONNECTIONS, before // 2)
            self._lane_failures.clear()
            LOG.warning(
                "%d upload connections lost within %.0f s; pool %d -> %d connections",
                recent, LANE_FAILURE_WINDOW_SECONDS, before, self._lane_target,
            )

    async def _close_extra_senders(self) -> None:
        senders, self._extra_senders = self._extra_senders, []
        self._extra_owner = None
        for sender in senders:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(sender.disconnect(), 5)

    def _discard_extra_sender(self, sender: MTProtoSender, reason: object) -> None:
        LOG.warning("Extra upload connection dropped (%s); its parts go over the main connection", reason)
        self._extra_retry_at = time.monotonic() + EXTRA_CONNECTION_RETRY_SECONDS
        self._note_lane_failure()
        if sender in self._extra_senders:
            self._extra_senders.remove(sender)
        asyncio.ensure_future(self._safe_disconnect_sender(sender))

    @staticmethod
    async def _safe_disconnect_sender(sender: MTProtoSender) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(sender.disconnect(), 5)

    async def _upload_big_parallel(
        self, client: TelegramClient, path: Any, size: int, handle: UploadHandle
    ) -> types.InputFileBig:
        """Big-file upload with several parts in flight over several connections.

        Telegram limits what one connection carries, so parts are spread over the client's own connection and a few
        extra ones to the same DC. A failing extra connection is dropped and its parts go over the main one; any
        failure of the main connection cancels the rest and restarts the file later.
        """
        part_size = UPLOAD_PART_SIZE_BYTES
        part_count = (size + part_size - 1) // part_size
        file_id = _random_id()
        lanes: list[MTProtoSender | None] = [None, *await self._extra_upload_senders(client)]  # None = own connection

        async def send(index: int, data: bytes, lane: MTProtoSender | None) -> None:
            request = functions.upload.SaveBigFilePartRequest(file_id, index, part_count, data)
            ok: Any = None
            if lane is not None:
                try:
                    ok = await asyncio.wait_for(lane.send(request), REQUEST_TIMEOUT_SECONDS)
                except errors.FloodError:
                    raise
                except asyncio.CancelledError:
                    if task_is_cancelling():
                        raise
                    self._forget_lane(lanes, lane, "connection lost")
                except (asyncio.TimeoutError, OSError, errors.RPCError) as exc:
                    self._forget_lane(lanes, lane, exc or type(exc).__name__)
            if ok is None:
                try:
                    ok = await asyncio.wait_for(client(request), REQUEST_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    # A part nobody confirms means the link is dead: say so, so the queue waits instead of burning attempts.
                    if client is self.client:
                        self._drop_connection(f"Telegram did not confirm file part {index + 1}/{part_count}")
                    raise
            if not ok:
                raise RuntimeError(f"Telegram refused file part {index + 1}/{part_count}")
            handle.advance(len(data))

        pending: set[asyncio.Future[None]] = set()
        try:
            with path.open("rb") as stream:
                for index in range(part_count):
                    data = stream.read(part_size)
                    while len(pending) >= self.parallel_parts * len(lanes):
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for finished in done:
                            finished.result()
                    pending.add(asyncio.ensure_future(send(index, data, lanes[index % len(lanes)])))
            if pending:
                await asyncio.gather(*pending)
        except BaseException:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        return types.InputFileBig(id=file_id, parts=part_count, name=path.name)

    def _forget_lane(self, lanes: list[MTProtoSender | None], lane: MTProtoSender, reason: object) -> None:
        if lane in lanes:
            lanes.remove(lane)
            self._discard_extra_sender(lane, reason)

    async def _upload_with_watchdog(self, client: TelegramClient, path: Any, handle: UploadHandle) -> Any:
        """upload_file with a stall detector: no new part for UPLOAD_STALL_SECONDS means a dead connection."""
        task = asyncio.ensure_future(self._upload_file(client, path, handle))
        last_sent = handle.sent
        idle_since = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=UPLOAD_WATCH_INTERVAL_SECONDS)
                if done:
                    return task.result()
                if handle.sent != last_sent:
                    last_sent, idle_since = handle.sent, time.monotonic()
                elif time.monotonic() - idle_since > UPLOAD_STALL_SECONDS:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    if client is self.client:
                        self._drop_connection(f"upload stalled for {UPLOAD_STALL_SECONDS:.0f}s")
                    raise ConnectionError(f"upload stalled for {UPLOAD_STALL_SECONDS:.0f}s")
        except asyncio.CancelledError:
            task.cancel()
            raise


# --- first-run sign-in (plain console, before the bot starts) ----------------------------------------------


async def _qr_login(client: TelegramClient, say: Callable[[str], None], attempts: int) -> None:
    import qrcode

    for attempt in range(1, attempts + 1):
        qr = await client.qr_login()
        timeout = max(0.0, (qr.expires - datetime.now(timezone.utc)).total_seconds())
        waiting = asyncio.create_task(qr.wait(timeout=timeout))
        try:
            await asyncio.sleep(0)  # let Telethon register its login handler before the code is shown
            code = qrcode.QRCode(border=2)
            code.add_data(qr.url)
            code.make(fit=True)
            buffer = io.StringIO()
            code.print_ascii(out=buffer, invert=True)
            say("")
            say("Вход в Telegram: Настройки → Устройства → Подключить устройство.")
            say("Отсканируйте QR-код аккаунтом, с которого пойдёт отправка (Ctrl+C — пропустить). Резервная ссылка:")
            say(qr.url)
            say("")
            for row in buffer.getvalue().splitlines():
                say(row)
            await waiting
            client.session.save()
            return
        except (asyncio.TimeoutError, TimeoutError):
            say(f"Срок действия QR-кода истёк, создаю новый ({attempt}/{attempts}).")
        except errors.SessionPasswordNeededError:
            password = await asyncio.to_thread(getpass.getpass, "Облачный пароль Telegram (ввод скрыт): ")
            await client.sign_in(password=password)
            client.session.save()
            return
        except errors.FloodWaitError as exc:
            raise RuntimeError(f"Telegram ограничил вход. Подождите {exc.seconds} с.") from exc
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    raise RuntimeError("QR-код истёк, попытки закончились.")


async def _preflight(settings: TelegramSettings, say: Callable[[str], None], attempts: int) -> str:
    client = new_client(settings, connection_retries=1, receive_updates=True)
    try:
        await asyncio.wait_for(client.connect(), 20)
        if not await asyncio.wait_for(client.is_user_authorized(), 20):
            await _qr_login(client, say, attempts)
        me = await asyncio.wait_for(client.get_me(), 20)
        if settings.account_id and int(me.id) != settings.account_id:
            say(f"Вошли под другим аккаунтом: {display_name(me)}. Нужен аккаунт с ID {settings.account_id}.")
            say("Выхожу из этой сессии, чтобы вы могли войти нужным аккаунтом.")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.log_out(), 20)
            raise WrongAccount("вход выполнен не тем аккаунтом")
        premium = " · Premium" if getattr(me, "premium", False) else " · без Premium"
        return f"{display_name(me)}{premium}"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def sign_in_if_needed(
    settings: TelegramSettings,
    say: Callable[[str], None] = print,
    *,
    attempts: int = 10,
) -> tuple[bool, str]:
    """Make sure the session is authorized. Returns (signed_in, message); never raises for network or skipped sign-in."""
    try:
        name = asyncio.run(_preflight(settings, say, attempts))
    except KeyboardInterrupt:
        return False, "вход в Telegram пропущен"
    except WrongAccount as exc:
        return False, str(exc)
    except (OSError, ConnectionError, asyncio.TimeoutError, TimeoutError) as exc:
        return False, f"нет связи с Telegram ({type(exc).__name__})"
    except Exception as exc:
        return False, f"вход в Telegram не выполнен: {exc}"
    return True, name
