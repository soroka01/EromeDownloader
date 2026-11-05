"""The pool of extra connections ("lanes") that carry parts of big files next to the client's own connection.

One connection of Telegram carries only a few MB/s, so the parts of a big file are spread over many connections to the
same data centre. `UploadLanes` owns those connections: it opens them on demand, replaces the ones that the other side
closed while they sat idle, drops the ones that fail, and finds the size of the pool by probing (up while things are
quiet, halved when Telegram drops connections). Sending the parts themselves is the business of `Transport`.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
from typing import Any, Callable, Coroutine

from telethon import TelegramClient, functions
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER

from erome.telegram.client import task_is_cancelling

LOG = logging.getLogger("erome.tg")
_LANE_LOG = logging.getLogger("erome.tg.lane")
_LANE_LOG.addHandler(logging.NullHandler())
_LANE_LOG.propagate = False


class _LaneLoggers(dict):
    """Loggers of the extra upload connections. Telethon words the ordinary end of such a connection (the server, or
    something between, closing it) as a WARNING and an ERROR; a real loss is reported by the pool in its own words."""

    def __missing__(self, key: str) -> logging.Logger:
        return _LANE_LOG


LANE_OPEN_TIMEOUT_SECONDS = 60.0  # dialling and the handshake of one lane (30 s each at most)
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


def without_updates(client: TelegramClient, request: Any) -> Any:
    """Telethon wraps every request of an update-less client like this; a connection of our own must do the same,
    or the server subscribes it to the account's updates (up to two dozen extra streams that nobody reads)."""
    return functions.InvokeWithoutUpdatesRequest(request) if getattr(client, "_no_updates", False) else request


async def quiet_disconnect(sender: MTProtoSender) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(sender.disconnect(), 5)


class UploadLanes:
    """The extra upload connections of one `Transport`. `cap` gives the configured maximum of connections (read each
    time, the setting may change), `spawn` runs a clean-up in the background and keeps it referenced until it is done."""

    def __init__(self, cap: Callable[[], int], spawn: Callable[[Coroutine[Any, Any, None]], None]) -> None:
        self._cap = cap
        self._spawn = spawn
        self.senders: list[MTProtoSender] = []
        self._owner: TelegramClient | None = None
        self._lock = asyncio.Lock()
        self.retry_at = 0.0
        self.failures: list[float] = []
        self.target = 0  # current size of the pool (0 = not chosen yet)
        self.quiet_since = time.monotonic()

    async def open_one(self, client: TelegramClient) -> MTProtoSender:
        """One more connection to the client's own DC with the same auth key (parts of one file are spread over them)."""
        dc = await client._get_dc(client.session.dc_id)
        # No automatic reconnection: a lane that breaks is dropped (its parts go over the main connection); two
        # dozen senders redialling at once is what turns a hiccup into a flood.
        loggers = _LaneLoggers()
        sender = MTProtoSender(client.session.auth_key, loggers=loggers, auto_reconnect=False, retries=1)
        try:
            await sender.connect(
                client._connection(
                    dc.ip_address, dc.port, dc.id, loggers=loggers, proxy=client._proxy, local_addr=client._local_addr
                )
            )
            init = copy.copy(client._init_request)
            init.query = functions.help.GetConfigRequest()
            await sender.send(without_updates(client, functions.InvokeWithLayerRequest(LAYER, init)))
        except BaseException:
            with contextlib.suppress(Exception):
                await sender.disconnect()
            raise
        return sender

    async def acquire(self, client: TelegramClient) -> list[MTProtoSender]:
        """Extra upload connections, created on demand and reused. Failing to get them just means fewer lanes."""
        wanted = self.effective_connections() - 1
        if wanted <= 0:
            return []
        async with self._lock:
            if self._owner is not client:
                await self.close()
                self._owner = client
            # A connection that the other side closed while it sat idle says nothing about overload (a loss during an
            # upload does): it is replaced without counting against the pool.
            for idle in [sender for sender in self.senders if not sender.is_connected()]:
                self.senders.remove(idle)
                self._spawn(quiet_disconnect(idle))
                LOG.debug("An idle upload connection was closed from outside; opening a new one")
            while len(self.senders) > wanted:  # the pool was halved: let the surplus go
                surplus = self.senders.pop()
                self._spawn(quiet_disconnect(surplus))
            missing = wanted - len(self.senders)
            if missing > 0 and time.monotonic() >= self.retry_at:
                # Opened side by side: one after another, two dozen handshakes would delay the first file by a minute.
                openings = [
                    asyncio.ensure_future(asyncio.wait_for(self.open_one(client), LANE_OPEN_TIMEOUT_SECONDS))
                    for _ in range(missing)
                ]
                try:
                    results = await asyncio.gather(*openings, return_exceptions=True)
                except BaseException:  # cancelled while dialling: close what had already connected
                    for opening in openings:
                        opening.cancel()
                    for late in await asyncio.gather(*openings, return_exceptions=True):
                        if isinstance(late, MTProtoSender):
                            self._spawn(quiet_disconnect(late))
                    raise
                failures = 0
                for result in results:
                    if isinstance(result, MTProtoSender):
                        self.senders.append(result)
                    elif isinstance(result, asyncio.CancelledError) and task_is_cancelling():
                        raise result
                    else:
                        failures += 1
                if failures:
                    LOG.warning(
                        "%d of %d extra upload connections failed to open (%s); uploading over fewer connections",
                        failures, missing, next((repr(r) for r in results if isinstance(r, BaseException)), ""),
                    )
                    self.retry_at = time.monotonic() + EXTRA_CONNECTION_RETRY_SECONDS
                LOG.info("Telegram upload connections: %d", 1 + len(self.senders))
            return list(self.senders)

    def effective_connections(self) -> int:
        """How many connections to use now: probed upwards from a safe start, halved after losses."""
        cap = max(1, self._cap())
        now = time.monotonic()
        if self.target <= 0:
            self.target = min(cap, UPLOAD_CONNECTIONS_START)
            self.quiet_since = now
        elif self.target < cap and now - self.quiet_since >= UPLOAD_CONNECTIONS_GROW_AFTER_SECONDS:
            self.target = min(cap, self.target + UPLOAD_CONNECTIONS_STEP)
            self.quiet_since = now
            LOG.info("Telegram upload connections raised to %d (cap %d)", self.target, cap)
        self.target = min(self.target, cap)
        return self.target

    def note_failure(self) -> None:
        now = time.monotonic()
        self.failures.append(now)
        del self.failures[:-16]
        self.quiet_since = now
        recent = sum(1 for stamp in self.failures if now - stamp <= LANE_FAILURE_WINDOW_SECONDS)
        if recent >= LANE_FAILURE_LIMIT:
            before = self.target or self._cap()
            self.target = max(MIN_UPLOAD_CONNECTIONS, before // 2)
            self.failures.clear()
            LOG.warning(
                "%d upload connections lost within %.0f s; pool %d -> %d connections",
                recent, LANE_FAILURE_WINDOW_SECONDS, before, self.target,
            )

    async def close(self) -> None:
        senders, self.senders = self.senders, []
        self._owner = None
        await asyncio.gather(*(quiet_disconnect(sender) for sender in senders))  # all at once, not 5 s each

    def discard(self, sender: MTProtoSender, reason: object) -> None:
        LOG.warning("Extra upload connection dropped (%s); its parts go over the main connection", reason)
        self.retry_at = time.monotonic() + EXTRA_CONNECTION_RETRY_SECONDS
        self.note_failure()
        if sender in self.senders:
            self.senders.remove(sender)
        self._spawn(quiet_disconnect(sender))
