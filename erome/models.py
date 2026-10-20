from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from erome.config import UNAVAILABLE_STATUSES


class Outcome(StrEnum):
    """How a download of a file, an album or a link ended; the values are what the manifest and the logs show."""

    SUCCESS = "success"
    FAILED = "failed"
    BANNED = "banned"  # the site says it is gone or closed
    SKIPPED = "skipped"  # the file was already on disk
    STOPPED = "stopped"  # not started: the user asked to stop


class LinkKind(StrEnum):
    """What a link points to; the values are also stored in the Telegram queue."""

    DIRECT = "direct"  # a file (or anything that is not an Erome album or profile)
    ALBUM = "album"
    ACCOUNT = "account"  # a profile


DownloadResultCallback = Callable[[str, Outcome], None]


FileResultCallback = Callable[["DownloadResult"], None]


@dataclass(slots=True)
class FileJob:
    url: str
    path: Path


@dataclass(slots=True)
class DownloadResult:
    url: str
    status: Outcome
    path: Path | None = None
    error: str = ""
    attempts: int = 0
    size_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (Outcome.SUCCESS, Outcome.SKIPPED)


@dataclass(slots=True)
class DownloadSummary:
    results: list[DownloadResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok_count(self) -> int:
        return sum(result.ok for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status == Outcome.FAILED for result in self.results)

    @property
    def banned_count(self) -> int:
        return sum(result.status == Outcome.BANNED for result in self.results)

    @property
    def total_size(self) -> int:
        return sum(result.size_bytes for result in self.results if result.ok)

    def overall_status(self) -> Outcome:
        if not self.results:
            return Outcome.FAILED
        if self.failed_count == 0 and self.banned_count == 0:
            return Outcome.SUCCESS
        if self.ok_count == 0 and self.banned_count > 0:
            return Outcome.BANNED
        return Outcome.FAILED


@dataclass(slots=True)
class AlbumData:
    url: str
    title: str
    urls: list[str]
    video_urls: list[str]
    image_urls: list[str]
    size_bytes: int | None = None
    sized_media_count: int = 0
    unknown_size_count: int = 0

    @property
    def video_count(self) -> int:
        return len(self.video_urls)

    @property
    def image_count(self) -> int:
        return len(self.image_urls)

    @property
    def media_count(self) -> int:
        return len(self.urls)

    @property
    def is_photo_only(self) -> bool:
        return self.image_count > 0 and self.video_count == 0

    @property
    def has_full_size(self) -> bool:
        return self.size_bytes is not None and self.unknown_size_count == 0


class DownloadError(Exception):
    pass


class AlbumFetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def unavailable(self) -> bool:
        """The site says the page is gone or closed (403, 404, 410), as opposed to a failure that may pass."""
        return self.status in UNAVAILABLE_STATUSES

    @property
    def outcome(self) -> Outcome:
        """How a link that could not be read ends: banned when the site says it is gone, failed otherwise."""
        return Outcome.BANNED if self.unavailable else Outcome.FAILED
