"""The Telegram connection of the Telegram mode: one long-lived Telethon client and everything around it.

`Transport` knows how to stay connected (a supervisor with deadlines on every network step), how to make a request and
how to move the bytes of a file over several connections; what is sent and how it is arranged into messages is the
business of `Userbot` (userbot.py), the first sign-in is signin.py, what a failure means is faults.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from telethon import TelegramClient, errors, functions, types, utils
from telethon.network import MTProtoSender

from erome.telegram.client import new_client, task_is_cancelling
from erome.telegram.faults import SESSION_ERRORS, RetryLater, UserbotUnavailable, WrongAccount, describe_error
from erome.telegram.lanes import UploadLanes, without_updates
from erome.telegram.names import display_name
from erome.telegram.settings import TelegramSettings

LOG = logging.getLogger("erome.tg")
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_OFFLINE = "offline"
STATE_UNAUTHORIZED = "unauthorized"
STATE_ERROR = "error"

# A connection that Telegram's side (or something in between) silently stops answering must not freeze the
# userbot forever: every network step has a deadline, and a stalled connection is dropped and rebuilt.
CONNECT_TIMEOUT_SECONDS = 30.0
HANDSHAKE_TIMEOUT_SECONDS = 30.0
RESOLVE_TIMEOUT_SECONDS = 180.0  # a fresh session may have to walk many dialogs to find the group
REQUEST_TIMEOUT_SECONDS = 90.0
UPLOAD_STALL_SECONDS = 150.0  # no new file part for this long = the connection is dead
UPLOAD_WATCH_INTERVAL_SECONDS = 5.0

# Parts of a big file are spread over MANY connections to the same DC, a few parts each (one connection carries
# only ~1-3 MB/s; 8+ parts in flight on one connection is slower than 3). Measured on a Premium account:
#   6 connections x 8 parts 7.5 MB/s | 16 x 3 10.0 | 24 x 3 15.8 | 32+ broke: corrupted packets.
DEFAULT_PARALLEL_PARTS = 3
# Telegram counts requests, not bytes: 512 KB parts (the maximum) halve the request rate of 256 KB ones.
UPLOAD_PART_SIZE_BYTES = 512 * 1024
BIG_FILE_THRESHOLD_BYTES = 10 * 1024 * 1024
PARALLEL_UPLOAD_MAX_BYTES = 2_000_000_000


@dataclass
class UploadHandle:
    """Live state of one file upload; the stall watchdog and the progress meter read it."""

    name: str
    total: int
    sent: int = 0
    listener: Callable[[int], None] | None = None

    def advance(self, count: int) -> None:
        self.sent += count
        if self.listener is not None and count > 0:
            self.listener(count)

    def set(self, sent: int) -> None:
        delta = sent - self.sent
        self.sent = sent
        if self.listener is not None and delta > 0:
            self.listener(delta)


class _Unauthorized(Exception):
    pass


def _random_id() -> int:
    value = int.from_bytes(os.urandom(8), "big", signed=True)
    return value or 1


class Transport:
    """The connection: a supervised Telethon client, requests with deadlines, file uploads over several connections."""

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
        self.lanes = UploadLanes(cap=lambda: self.upload_connections, spawn=self._spawn)
        self.client: TelegramClient | None = None
        self._peer: Any = None
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._background: set[asyncio.Task[Any]] = set()  # fire-and-forget clean-ups: the loop only holds weak references

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
        await self.lanes.close()
        if self.client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.client.disconnect(), 10)
        if self._background:
            await asyncio.wait(list(self._background), timeout=10)

    def _spawn(self, coroutine: Any) -> None:
        """Run a clean-up in the background and keep it referenced until it is done."""
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

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
        if not self.ready:
            # A stale event: the client dropped and the supervisor has not noticed yet. Returning at once would let a
            # caller that loops on this call spin without ever giving the event loop back (and the supervisor, which
            # would clear the state, never runs), so hand control over for a moment.
            self._ready.clear()
            await asyncio.sleep(0.2)
        return self.ready

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

    def panel_status(self) -> str:
        """The same for the bot chat: no file names or console instructions, only what the owner can act upon."""
        if self.state == STATE_UNAUTHORIZED:
            return "вход в аккаунт отозван — войдите заново на компьютере, где запущена программа"
        if self.state == STATE_ERROR:
            return "ошибка настроек — подробности в консоли программы"
        return self.status_text()

    def _set_state(self, state: str, error: str = "") -> None:
        # A new reason for the same offline state is not news: the failed attempt behind it is logged on its own.
        if state != self.state or (error != self.error and state != STATE_OFFLINE):
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
            self._spawn(self._safe_disconnect(client))
        self._spawn(self.lanes.close())

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
        if self.thread_id:
            if isinstance(entity, types.Channel) and getattr(entity, "forum", False):
                await self._check_topic(client)
            else:
                LOG.warning(
                    "thread_id %s is set, but the group is not a forum: every file will be sent as a reply to the message "
                    "with that number",
                    self.thread_id,
                )

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
            raise self.flood_wait(exc, type(request).__name__) from exc

    def flood_wait(self, exc: BaseException, what: str) -> RetryLater:
        """Remember a flood wait (it holds back every request, not just this one) and build the report of it."""
        seconds = int(getattr(exc, "seconds", 0) or 30)
        self.flood_until = max(self.flood_until, time.monotonic() + seconds + 1)
        LOG.warning("Telegram flood wait of %s s on %s", seconds, what)
        return RetryLater(seconds + 1, f"Telegram flood wait {seconds}s")

    # --- upload connections -------------------------------------------------
    async def _upload_file(self, client: TelegramClient, path: Any, handle: UploadHandle) -> Any:
        size = handle.total
        if self.parallel_parts > 1 and BIG_FILE_THRESHOLD_BYTES < size <= PARALLEL_UPLOAD_MAX_BYTES:
            return await self._upload_big_parallel(client, path, size, handle)
        return await client.upload_file(
            path, file_name=path.name, progress_callback=lambda sent, _total: handle.set(sent)
        )

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
        lanes: list[MTProtoSender | None] = [None, *await self.lanes.acquire(client)]  # None = own connection

        async def send(index: int, data: bytes, lane: MTProtoSender | None) -> None:
            request = functions.upload.SaveBigFilePartRequest(file_id, index, part_count, data)
            ok: Any = None
            if lane is not None:
                try:
                    ok = await asyncio.wait_for(
                        lane.send(without_updates(client, request)), REQUEST_TIMEOUT_SECONDS
                    )
                except errors.FloodError:
                    raise
                except asyncio.CancelledError:
                    if task_is_cancelling():
                        raise
                    self._forget_lane(lanes, lane, "connection lost")
                except Exception as exc:  # any failure of an extra connection (a closed stream, a transport flood, ...)
                    self._forget_lane(lanes, lane, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
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
                        self._raise_first_failure(done)
                    pending.add(asyncio.ensure_future(send(index, data, lanes[index % len(lanes)])))
            if pending:
                await asyncio.gather(*pending)
        except BaseException:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        return types.InputFileBig(id=file_id, parts=part_count, name=path.name)

    @staticmethod
    def _raise_first_failure(done: set[asyncio.Future[None]]) -> None:
        """Raise the first error of the finished part tasks; the others are read too, so none is left unretrieved."""
        failures: list[BaseException] = []
        for finished in done:
            try:
                finished.result()
            except BaseException as exc:  # noqa: BLE001 - collected, the first one is raised below
                failures.append(exc)
        if failures:
            raise failures[0]

    def _forget_lane(self, lanes: list[MTProtoSender | None], lane: MTProtoSender, reason: object) -> None:
        if lane in lanes:
            lanes.remove(lane)
            self.lanes.discard(lane, reason)

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
                    try:
                        await task
                    except asyncio.CancelledError:
                        if task_is_cancelling():  # the cancellation of the upload is expected, one aimed at us is not
                            raise
                    except Exception:
                        pass
                    if client is self.client:
                        self._drop_connection(f"upload stalled for {UPLOAD_STALL_SECONDS:.0f}s")
                    raise ConnectionError(f"upload stalled for {UPLOAD_STALL_SECONDS:.0f}s")
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)  # let the upload close its file before the caller goes on
            raise
