"""Telegram userbot: posts the downloaded media to the target topic.

Photos and videos go out as albums (media groups of up to 10), anything else as single messages. Every message
uses a deterministic random_id, so repeating a batch after a lost answer or a restart can never post a file twice.
The connection itself (supervisor, deadlines, upload connections, sign-in) lives in tg_transport.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from telethon import TelegramClient, errors, functions, types, utils

from erome.tg_media import SendItem
from erome.tg_transport import (
    BIG_FILE_THRESHOLD_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    SESSION_ERRORS,
    RetryLater,
    Transport,
    UploadHandle,
    UserbotUnavailable,
    describe_error,
    error_classes,
    error_code,
    human_error,
)
from erome.ui import human_size

LOG = logging.getLogger("erome.tg")

# Telegram: 2000 MiB for regular accounts, 4000 MiB for Premium. Slightly lower decimal values keep a margin.
FREE_UPLOAD_LIMIT_BYTES = 2_000_000_000
PREMIUM_UPLOAD_LIMIT_BYTES = 4_000_000_000
ALBUM_MAX_ITEMS = 10

SEND_TIMEOUT_SECONDS = 120.0  # the answer to a send request (an album of ten may take a while)

# An uploaded file stays usable on Telegram's side for a while; a repeated attempt (flood wait, closed topic, a lost
# answer) reuses the handle instead of sending the whole file again.
UPLOAD_REUSE_SECONDS = 1800.0

SMALL_UPLOAD_CONCURRENCY = 4  # files of up to 10 MB are uploaded this many at a time


class DeliveryUncertain(RuntimeError):
    """The send request may have reached Telegram; repeating it is safe (same random_id)."""


class DestinationError(RuntimeError):
    """The target chat/topic refuses our messages (no rights, closed topic, ...): waiting may fix it, retrying will not."""


class GroupRejected(RuntimeError):
    """Telegram refused the whole album; nothing was posted and the files can still go out one by one."""


DESTINATION_ERRORS = error_classes(
    "ChatWriteForbiddenError", "ChatAdminRequiredError", "UserBannedInChannelError", "ChannelPrivateError",
    "ChannelInvalidError", "PeerIdInvalidError", "TopicDeletedError", "ChatRestrictedError",
    "ChatSendMediaForbiddenError", "ChatSendPhotosForbiddenError", "ChatSendVideosForbiddenError",
    "ChatSendDocsForbiddenError", "ChatSendGifsForbiddenError", "ChatSendPlainForbiddenError",
)
PHOTO_ERRORS = error_classes(
    "PhotoInvalidDimensionsError", "PhotoSaveFileInvalidError", "ImageProcessFailedError", "PhotoExtInvalidError",
    "PhotoCropSizeSmallError", "PhotoInvalidError", "MediaInvalidError",
)
# A kept upload handle that Telegram no longer knows (expired, other DC, parts lost): upload the file again.
STALE_UPLOAD_ERRORS = error_classes(
    "FilePartMissingError", "FilePartsInvalidError", "FileIdInvalidError", "FilePartInvalidError",
    "FilePart0MissingError", "FilePartLengthInvalidError", "FilePartSizeInvalidError", "FilePartSizeChangedError",
)
# Telegram's own hiccups (HTTP 500 family). Telethon retries them five times and, with raise_last_call_error, raises
# the last one: RANDOM_ID_DUPLICATE is one of them and means "an earlier attempt of this message went through".
TRANSIENT_RPC_ERRORS = error_classes(
    "ServerError", "RpcCallFailError", "RpcMcgetFailError", "InterdcCallErrorError", "InterdcCallRichErrorError",
    "TimedOutError",
)
# Trouble with the link, not with the data: the same request can simply be repeated later.
TRANSIENT_ERRORS = (ConnectionError, asyncio.TimeoutError, DeliveryUncertain, UserbotUnavailable, *TRANSIENT_RPC_ERRORS)


def is_destination_error(exc: BaseException) -> bool:
    return isinstance(exc, DESTINATION_ERRORS) or "TOPIC_CLOSED" in str(getattr(exc, "message", "") or exc)


def is_file_error(exc: BaseException) -> bool:
    """A refusal about the media itself (rejected media or picture), not about the chat, the account or the link."""
    code = error_code(exc)
    return code.startswith(("MEDIA_", "PHOTO_")) or code == "IMAGE_PROCESS_FAILED"


class Refusal(str):
    """Why Telegram would not take a file. `lasting` means the file itself is at fault - not the moment, the chat or
    the connection - so asking again would get the same answer and the file need not be downloaded again."""

    lasting: bool

    def __new__(cls, text: str, lasting: bool = False) -> "Refusal":
        reason = super().__new__(cls, text)
        reason.lasting = lasting
        return reason


class Userbot(Transport):
    """The transport plus the sending side: uploads, albums, single messages, the rules of what counts as delivered."""

    @property
    def max_upload_bytes(self) -> int:
        return PREMIUM_UPLOAD_LIMIT_BYTES if self.premium else FREE_UPLOAD_LIMIT_BYTES

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

        Returns {file key: Refusal} for the files Telegram refused; `Refusal.lasting` tells whether the file itself is
        at fault. Delivered files are reported through `on_delivered` the moment Telegram accepts them, so a repeated
        call only needs to carry the rest.
        Raises UserbotUnavailable, RetryLater, DestinationError or the underlying network error; none of these
        means that anything was posted twice, because every message keeps its random_id. Uploaded file handles
        stay on the items, so a repeated call does not send the bytes again.
        """
        refused: dict[str, str] = {}
        sendable: list[SendItem] = []
        for item in items:
            if item.size <= 0:
                refused[item.key] = Refusal("пустой файл")  # may be a bad download: worth another try later
            elif item.size > self.max_upload_bytes:
                refused[item.key] = Refusal(f"файл больше лимита Telegram ({human_size(self.max_upload_bytes)})", lasting=True)
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
            if item.file is None or time.monotonic() - item.uploaded_at >= UPLOAD_REUSE_SECONDS:
                item.file = item.thumb_file = None
                handle = UploadHandle(name=item.path.name, total=item.size, listener=on_bytes)
                try:
                    file = await self._upload_with_watchdog(client, item.path, handle)
                except (errors.FloodError, *SESSION_ERRORS, *TRANSIENT_RPC_ERRORS):
                    raise
                except errors.RPCError as exc:  # this very file is refused: the others still go out
                    rejected[item.key] = Refusal(human_error(exc), lasting=is_file_error(exc))
                    return
                # Kept at once: a thumbnail that fails below must not make a repeated attempt send the whole file again.
                item.file, item.uploaded_at = file, time.monotonic()
            if item.thumb is None or item.thumb_file is not None or item.thumb_refused:
                return
            try:
                item.thumb_file = await asyncio.wait_for(client.upload_file(item.thumb), REQUEST_TIMEOUT_SECONDS)
            except (errors.FloodError, *SESSION_ERRORS, *TRANSIENT_RPC_ERRORS):
                raise
            except errors.RPCError as exc:
                item.thumb_refused = True
                LOG.info("Thumbnail of %s was not accepted (%s); sending without it", item.path.name, describe_error(exc))

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
            raise self.flood_wait(exc, "file upload") from exc
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

    async def _send_single(self, item: SendItem, caption: str) -> Refusal | None:
        """Post one file. None = delivered; otherwise the reason Telegram refused it."""
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
            return Refusal(human_error(exc))  # twice in a row, but it may be the link that corrupts uploads: not lasting
        except errors.RPCError as exc:
            if is_destination_error(exc):
                raise DestinationError(human_error(exc)) from exc
            if item.kind == "photo" and isinstance(exc, PHOTO_ERRORS):
                LOG.info("Telegram refused %s as a photo (%s); sending it as a file", item.path.name, describe_error(exc))
                item.kind = "file"
                return await self._send_single(item, caption)
            return Refusal(human_error(exc), lasting=is_file_error(exc))
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
                    raise DestinationError(human_error(exc)) from exc
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
                raise DestinationError(human_error(exc)) from exc
            raise GroupRejected(describe_error(exc)) from exc
        except (asyncio.CancelledError, UserbotUnavailable, RetryLater):
            raise
        except Exception as exc:
            raise DeliveryUncertain(f"{type(exc).__name__}: {exc}") from exc
