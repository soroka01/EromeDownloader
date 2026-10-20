"""Facts about downloaded files that Telegram needs: kind, image size, video metadata and a thumbnail.

Video facts come from the ffmpeg binary bundled with `imageio-ffmpeg` (the project has no ffprobe); every
blocking call runs in a worker thread so the event loop (bot polling, Telegram connection) never stalls.
"""
from __future__ import annotations

import asyncio
import functools
import mimetypes
import os
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov"})
# Telegram accepts a "photo" only up to 10 MB, with width + height <= 10000 and a side ratio <= 20.
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_MAX_SIDES_SUM = 10_000
PHOTO_MAX_RATIO = 20

PROBE_TIMEOUT_SECONDS = 60
THUMBNAIL_TIMEOUT_SECONDS = 60
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?Video:.*?,\s*(\d{2,5})x(\d{2,5})")
_ROTATION_RE = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int


@dataclass
class SendItem:
    """One downloaded file ready to be sent."""

    key: str
    path: Path
    size: int
    random_id: int
    kind: str  # "photo" | "video" | "file"
    mime: str
    video: VideoInfo | None = None
    thumb: Path | None = None
    # Filled by the userbot: the uploaded file handles are kept here, so a repeated attempt does not upload again.
    file: Any = None
    thumb_file: Any = None
    uploaded_at: float = 0.0
    thumb_refused: bool = False  # Telegram did not take the thumbnail: the video goes out without one
    stale_resets: int = 0  # times Telegram refused the kept handle (the file is re-uploaded; twice = the file is bad)

    def cleanup_paths(self) -> list[Path]:
        return [path for path in (self.path, self.thumb) if path is not None]


@functools.cache
def ffmpeg_exe() -> str | None:
    """Path of the ffmpeg binary bundled with imageio-ffmpeg (looked up once); None when it is not available."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _run_ffmpeg(args: list[str], timeout: int) -> subprocess.CompletedProcess[bytes] | None:
    exe = ffmpeg_exe()
    if exe is None:
        return None
    try:
        return subprocess.run(
            [exe, "-hide_banner", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def probe_video(path: Path) -> VideoInfo | None:
    completed = _run_ffmpeg(["-i", str(path)], PROBE_TIMEOUT_SECONDS)
    if completed is None:
        return None
    text = completed.stderr.decode("utf-8", errors="replace")
    duration = _DURATION_RE.search(text)
    video = _VIDEO_RE.search(text)
    if duration is None or video is None:
        return None
    hours, minutes, seconds = duration.groups()
    width, height = int(video.group(1)), int(video.group(2))
    rotation = _ROTATION_RE.search(text)
    if rotation and round(abs(float(rotation.group(1)))) % 180 == 90:
        width, height = height, width  # the player shows the rotated picture
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return VideoInfo(total, width, height)


def make_thumbnail(path: Path, destination: Path, at_seconds: float = 1.0) -> Path | None:
    args = [
        "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, at_seconds):.1f}", "-i", str(path),
        "-frames:v", "1", "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease", str(destination),
    ]
    completed = _run_ffmpeg(args, THUMBNAIL_TIMEOUT_SECONDS)
    if completed is None or completed.returncode != 0:
        destination.unlink(missing_ok=True)
        return None
    if not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        return None
    return destination


def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of a PNG, GIF or JPEG read from its header; None when it cannot be told."""
    try:
        with path.open("rb") as stream:
            head = stream.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                width, height = struct.unpack(">II", head[16:24])
                return width, height
            if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
                width, height = struct.unpack("<HH", head[6:10])
                return width, height
            if head[:2] == b"\xff\xd8":
                return _jpeg_size(stream)
    except (OSError, struct.error):
        return None
    return None


def _jpeg_size(stream) -> tuple[int, int] | None:
    stream.seek(2)
    while True:
        marker = stream.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return None
        code = marker[1]
        while code == 0xFF:  # fill bytes before a marker
            extra = stream.read(1)
            if not extra:
                return None
            code = extra[0]
        if code == 0xD9:
            return None
        if code == 0x01 or 0xD0 <= code <= 0xD8:  # markers without a payload
            continue
        raw_length = stream.read(2)
        if len(raw_length) < 2:
            return None
        (length,) = struct.unpack(">H", raw_length)
        if code in _SOF_MARKERS:
            data = stream.read(5)
            if len(data) < 5:
                return None
            height, width = struct.unpack(">HH", data[1:5])
            return width, height
        stream.seek(max(0, length - 2), os.SEEK_CUR)


def photo_limits_ok(size: int, dimensions: tuple[int, int] | None) -> bool:
    if size > PHOTO_MAX_BYTES:
        return False
    if dimensions is None:
        return True  # unknown: try as a photo, the document fallback catches a refusal
    width, height = dimensions
    if width <= 0 or height <= 0:
        return False
    return width + height <= PHOTO_MAX_SIDES_SUM and max(width, height) / min(width, height) <= PHOTO_MAX_RATIO


def classify(path: Path, size: int) -> str:
    extension = path.suffix.lower()
    if extension in PHOTO_EXTENSIONS:
        return "photo" if photo_limits_ok(size, image_size(path)) else "file"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "file"


def mime_type(path: Path, kind: str) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed
    return "video/mp4" if kind == "video" else "application/octet-stream"


async def prepare_item(path: Path, key: str, random_id: int) -> SendItem:
    """Decide what a downloaded file is (after download, so real size and dimensions count) and gather its facts."""
    size = path.stat().st_size
    kind = await asyncio.to_thread(classify, path, size)
    item = SendItem(key=key, path=path, size=size, random_id=random_id, kind=kind, mime=mime_type(path, kind))
    if kind == "video":
        item.video = await asyncio.to_thread(probe_video, path)
        at = min(1.0, item.video.duration / 2) if item.video and item.video.duration else 0.0
        item.thumb = await asyncio.to_thread(
            make_thumbnail, path, path.with_name(f"{key}.thumb.jpg"), at
        )
    return item
