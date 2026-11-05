"""The Telethon client of the Telegram mode: one that never listens for updates, and the little helpers around it."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from telethon import TelegramClient, utils

from erome.telegram.settings import TelegramSettings

LOG = logging.getLogger("erome.tg")


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

    def _is_current(self, handle_name: str) -> bool:
        """False for a loop that a later `connect()` has replaced.

        `connect()` starts new loops and forgets the old ones without stopping them. Telethon's own loops end by
        themselves when the connection drops; these sleep first, so one that wakes up after a quick reconnect would
        see a connected client again and live on next to its successor - one more pair after every short outage.
        """
        handle = getattr(self, handle_name, None)
        return handle is None or handle is asyncio.current_task()

    async def _update_loop(self) -> None:
        queue = getattr(self, "_updates_queue", None)
        while self.is_connected() and self._is_current("_updates_handle"):
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
        while self.is_connected() and self._is_current("_keepalive_handle"):
            await asyncio.sleep(60)
            if not self._is_current("_keepalive_handle"):
                return
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
