from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DownloadResultCallback = Callable[[str, str], None]


FileResultCallback = Callable[["DownloadResult"], None]


@dataclass(slots=True)
class FileJob:
    url: str
    path: Path


@dataclass(slots=True)
class DownloadResult:
    url: str
    status: str
    path: Path | None = None
    error: str = ""
    attempts: int = 0
    size_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {"success", "skipped"}


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
        return sum(result.status == "failed" for result in self.results)

    @property
    def banned_count(self) -> int:
        return sum(result.status == "banned" for result in self.results)

    @property
    def total_size(self) -> int:
        return sum(result.size_bytes for result in self.results if result.ok)

    def overall_status(self) -> str:
        if not self.results:
            return "failed"
        if self.failed_count == 0 and self.banned_count == 0:
            return "success"
        if self.ok_count == 0 and self.banned_count > 0:
            return "banned"
        return "failed"


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
