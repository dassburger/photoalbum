"""EXIF/ffprobe date extraction for photos and videos, with mtime fallback."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import piexif

logger = logging.getLogger(__name__)

# Photo/video extensions this module knows how to read a capture date from.
# (The full publish-manifest whitelist, including which of these prep/assets.py
# actually generates thumbnails/posters for, is owned by Task 4/6 — this list
# only drives get_item_date()'s dispatch.)
PHOTO_EXTENSIONS = {".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}

# Timestamp formats ffprobe's format.tags.creation_time can come back as.
_FFPROBE_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
)


def is_video(filename: str) -> bool:
    """True if `filename`'s extension is one of VIDEO_EXTENSIONS.

    The single shared "is this a video" check -- prep/cli.py and
    prep/render.py each used to inline this same
    `Path(...).suffix.lower() in VIDEO_EXTENSIONS` expression (3 copies
    total); this module already owns VIDEO_EXTENSIONS/PHOTO_EXTENSIONS and
    the extension-based dispatch in get_item_date, so it's the natural
    single home for this check too (see code smells review finding #2).
    """
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def _mtime_date(path: Path) -> datetime:
    """Return a file's mtime as a naive local datetime."""
    return datetime.fromtimestamp(path.stat().st_mtime)


def get_photo_date(path: str | Path) -> tuple[datetime, bool]:
    """Read EXIF DateTimeOriginal from a photo, falling back to file mtime.

    Returns (datetime, is_fallback). EXIF DateTimeOriginal is naive local
    time, as stored by cameras, and is returned as-is (no tz conversion).
    Falls back to the file's mtime (with a logged warning) if the tag is
    missing or the file's EXIF data can't be read/parsed at all.
    """
    path = Path(path)
    try:
        exif_dict = piexif.load(str(path))
        raw = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("ascii")
            return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S"), False
    except Exception:
        logger.warning(
            "Could not read EXIF data from %s; falling back to mtime", path
        )
    else:
        logger.warning(
            "No EXIF DateTimeOriginal tag in %s; falling back to mtime", path
        )
    return _mtime_date(path), True


def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _ffprobe_creation_time(path: Path) -> str | None:
    """Return the raw format.tags.creation_time string via ffprobe, or None.

    Returns None (rather than raising) whenever ffprobe is unavailable, the
    file can't be probed, or the tag isn't present/parseable — all of these
    collapse to the same mtime-fallback behavior in get_video_date().
    """
    if not _ffprobe_available():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format_tags=creation_time",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data.get("format", {}).get("tags", {}).get("creation_time")


def get_video_date(path: str | Path) -> tuple[datetime, bool]:
    """Read the creation time from a video via ffprobe, falling back to mtime.

    ffprobe's creation_time is UTC; it is converted to the local timezone
    (then stripped of tzinfo, to match get_photo_date's naive-local
    convention) before being returned, so photos and videos from the same
    day sort together correctly.

    Returns (datetime, is_fallback). Falls back to the file's mtime (with a
    logged warning) if ffprobe isn't installed, the file can't be probed, or
    the tag is missing/unparseable.
    """
    path = Path(path)
    creation_time = _ffprobe_creation_time(path)
    if creation_time:
        for fmt in _FFPROBE_TIME_FORMATS:
            try:
                dt_utc = datetime.strptime(creation_time, fmt).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            return dt_utc.astimezone().replace(tzinfo=None), False
    logger.warning(
        "No usable video creation time for %s; falling back to mtime", path
    )
    return _mtime_date(path), True


def get_item_date(path: str | Path) -> tuple[datetime, bool]:
    """Dispatch to get_photo_date/get_video_date by file extension.

    Raises ValueError for an extension this module doesn't know how to date.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext in PHOTO_EXTENSIONS:
        return get_photo_date(path)
    if ext in VIDEO_EXTENSIONS:
        return get_video_date(path)
    raise ValueError(f"unsupported file extension for date extraction: {ext}")
