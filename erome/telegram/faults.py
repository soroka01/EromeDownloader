"""What can go wrong when talking to Telegram, and what each failure means.

The exceptions of the Telegram mode, the grouping of Telethon's RPC errors (the session is gone, the chat refuses us,
the file itself is bad, the link hiccuped) and the texts for the log and for the owner. Nothing here uses the network.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from telethon import errors

if TYPE_CHECKING:
    from erome.telegram.media import SendItem


class UserbotUnavailable(RuntimeError):
    """Telegram is not connected (yet); nothing was sent."""


class RetryLater(RuntimeError):
    """Telegram asked to wait (flood control); nothing was sent and it is not a failed attempt."""

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason or f"retry in {seconds:.0f}s")
        self.seconds = float(seconds)


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


def error_classes(*names: str) -> tuple[type[BaseException], ...]:
    """Telethon error classes by name; a name that the installed Telethon does not know is skipped."""
    return tuple(getattr(errors, name) for name in names if hasattr(errors, name))


# Telethon's specific error classes keep only the category in `.message`; the generic one keeps the RPC code there.
ERROR_CATEGORIES = frozenset(
    {"BAD_REQUEST", "UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "FLOOD", "INTERNAL", "SEE_OTHER", "NOT_ACCEPTABLE", "ABORTED"}
)
_CAUSE_SUFFIX_RE = re.compile(r"\s*\(caused by .*\)\s*$", re.DOTALL)  # greedy: the request in it has brackets of its own
_WORD_BREAK_RE = re.compile(r"(?<!^)(?=[A-Z])")


def error_code(exc: BaseException) -> str:
    """RPC code of a Telethon error (CHAT_WRITE_FORBIDDEN): a specific class carries it in its name."""
    message = str(getattr(exc, "message", "") or "")
    if message and message not in ERROR_CATEGORIES:
        return message.split(":", 1)[0].strip().upper()
    name = type(exc).__name__
    return _WORD_BREAK_RE.sub("_", name.removesuffix("Error")).upper()


def describe_error(exc: BaseException) -> str:
    """One line about a failure for logs: the RPC code of a generic error, else the code and Telethon's description."""
    message = str(getattr(exc, "message", "") or "")
    if message and message not in ERROR_CATEGORIES:
        return message[:160]
    text = _CAUSE_SUFFIX_RE.sub("", str(exc)).strip() or message
    if text and isinstance(exc, errors.RPCError):
        text = f"{error_code(exc)}: {text}"
    return text[:160] or type(exc).__name__


# What the owner reads in the bot panel and in job notes instead of a bare Telegram error code.
ERROR_TEXTS = {
    "CHAT_WRITE_FORBIDDEN": "в группу нельзя писать",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "CHAT_RESTRICTED": "группа ограничена для этого аккаунта",
    "USER_BANNED_IN_CHANNEL": "аккаунт заблокирован в группе",
    "CHANNEL_PRIVATE": "группа закрыта или аккаунта в ней больше нет",
    "CHANNEL_INVALID": "группа не найдена",
    "PEER_ID_INVALID": "группа не найдена",
    "TOPIC_CLOSED": "тема закрыта",
    "TOPIC_DELETED": "тема удалена",
    "CHAT_SEND_MEDIA_FORBIDDEN": "в группе запрещена отправка медиа",
    "CHAT_SEND_PHOTOS_FORBIDDEN": "в группе запрещена отправка фото",
    "CHAT_SEND_VIDEOS_FORBIDDEN": "в группе запрещена отправка видео",
    "CHAT_SEND_DOCS_FORBIDDEN": "в группе запрещена отправка файлов",
    "CHAT_SEND_GIFS_FORBIDDEN": "в группе запрещена отправка GIF",
    "CHAT_SEND_PLAIN_FORBIDDEN": "в группе запрещена отправка сообщений",
    "MEDIA_EMPTY": "Telegram счёл файл пустым или неподдерживаемым",
    "MEDIA_INVALID": "Telegram не принял файл",
    "PHOTO_INVALID": "Telegram не принял картинку",
    "PHOTO_INVALID_DIMENSIONS": "у картинки неподходящие размеры",
    "PHOTO_SAVE_FILE_INVALID": "Telegram не принял картинку",
    "PHOTO_EXT_INVALID": "формат картинки не подходит",
    "IMAGE_PROCESS_FAILED": "Telegram не смог обработать картинку",
    "FILE_PARTS_INVALID": "файл не загрузился целиком",
    "FILE_PART_MISSING": "файл не загрузился целиком",
}


def human_error(exc: BaseException) -> str:
    """`describe_error` for people: a short Russian phrase and the Telegram code in brackets."""
    code = error_code(exc)
    text = ERROR_TEXTS.get(code)
    return f"{text} ({code})" if text else code


class DeliveryUncertain(RuntimeError):
    """The send request may have reached Telegram; repeating it is safe (same random_id)."""


class DestinationError(RuntimeError):
    """The target chat/topic refuses our messages (no rights, closed topic, ...): waiting may fix it, retrying will not."""


class GroupRejected(RuntimeError):
    """Telegram refused the whole album; nothing was posted and the files can still go out one by one."""

    def __init__(self, reason: str, code: str = "", culprit: SendItem | None = None) -> None:
        super().__init__(reason)
        self.code = code  # the RPC code of the refusal when there was one (MEDIA_EMPTY, ...)
        self.culprit = culprit  # the file that was refused when Telegram said which one (the registration of the media)


# RPC codes that mean "this chat or topic refuses our messages". Matched by code, not by Telethon's class: the installed
# Telethon has no class for some of them (CHAT_SEND_DOCS_FORBIDDEN, TOPIC_CLOSED) and raises a generic error instead.
DESTINATION_CODES = frozenset({
    "CHAT_WRITE_FORBIDDEN", "CHAT_ADMIN_REQUIRED", "USER_BANNED_IN_CHANNEL", "CHANNEL_PRIVATE", "CHANNEL_INVALID",
    "PEER_ID_INVALID", "TOPIC_DELETED", "TOPIC_CLOSED", "CHAT_RESTRICTED", "USER_NOT_PARTICIPANT",
    "CHAT_SEND_MEDIA_FORBIDDEN", "CHAT_SEND_PHOTOS_FORBIDDEN", "CHAT_SEND_VIDEOS_FORBIDDEN",
    "CHAT_SEND_DOCS_FORBIDDEN", "CHAT_SEND_GIFS_FORBIDDEN", "CHAT_SEND_PLAIN_FORBIDDEN",
})
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
# Trouble with the link, not with the data: the same request can simply be repeated later (EOFError: a connection that
# the other side closed in the middle of an answer).
TRANSIENT_ERRORS = (
    ConnectionError, EOFError, asyncio.TimeoutError, DeliveryUncertain, UserbotUnavailable, *TRANSIENT_RPC_ERRORS
)


def is_destination_error(exc: BaseException) -> bool:
    return error_code(exc) in DESTINATION_CODES


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
