"""Thumbnail and video poster-frame generation, written into <album>/.web/thumbs/.

`generate_photo_thumbnail` and `generate_video_poster` each take an explicit
`dest` path (the caller — Task 11's `prep publish` orchestration — decides
where), but this module also owns the naming convention used for that `dest`
(`thumbnail_filename`/`thumbnail_path`) so that convention lives in one place
and `cleanup_stale_thumbnails` can use the exact same mapping to know which
generated files are still wanted.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageOps

from prep.atomic import atomic_write
from prep.manifest import ManifestItem

logger = logging.getLogger(__name__)

# Fixed subdirectory (relative to an album's own folder) that generated
# thumbnails/poster-frames are written into — never configurable, matching
# the plan's Solution Overview.
THUMBS_DIRNAME = ".web/thumbs"

DEFAULT_MAX_SIZE = 800

# How far into a video to seek before grabbing the poster frame. Early
# enough to avoid landing past a very short clip's end, late enough to
# usually skip a black first frame.
DEFAULT_POSTER_SEEK = "00:00:01"


class AssetError(Exception):
    """Raised when a source photo/video can't be read at all (hard failure).

    Per Development Approach's "fail loudly" rule, this is always a hard
    error (never silently skipped/warned) and always identifies the
    offending source filename.
    """


def thumbnail_filename(source_filename: str) -> str:
    """Return the generated thumbnail/poster filename for a source file.

    Always ends in `.jpg` (thumbnails and poster-frames are always written
    as JPEG) and keeps the *full* original filename, including its own
    extension, as a prefix — so e.g. `IMG_0001.jpg` (a photo) and
    `IMG_0001.mp4` (an unrelated video that happens to share a stem) never
    collide on the same generated path.

    Defensively takes only `Path(source_filename).name` (its basename) even
    though prep/manifest.py's `validate_manifest` already hard-rejects any
    `items` entry whose path escapes the album directory — belt-and-braces
    so a path separator (e.g. a stray `../` or `work/`) in `source_filename`
    can never translate into a generated-file path outside
    `thumbs_dir()`/a staging directory (see Quality review finding #1).
    """
    return f"{Path(source_filename).name}.jpg"


def thumbs_dir(album_dir: str | Path) -> Path:
    """Return the `.web/thumbs/` directory for an album (may not exist yet)."""
    return Path(album_dir) / THUMBS_DIRNAME


def thumbnail_path(album_dir: str | Path, source_filename: str) -> Path:
    """Return the full generated-thumbnail path for a source file in an album."""
    return thumbs_dir(album_dir) / thumbnail_filename(source_filename)


def generate_photo_thumbnail(
    src: str | Path, dest: str | Path, max_size: int = DEFAULT_MAX_SIZE
) -> tuple[int, int]:
    """Generate a thumbnail for a photo, honoring EXIF orientation.

    Applies `ImageOps.exif_transpose` *before* resizing, so a portrait
    photo carrying an EXIF Orientation tag isn't rotated in the generated
    thumbnail. Written atomically (temp file + rename, see `atomic_write`)
    to `dest`, which is created (along with any missing parent
    directories) if needed.

    Returns the thumbnail's final `(width, height)`, for Task 5's
    aspect-ratio input.

    Raises `AssetError`, identifying `src`, if Pillow can't read it as an
    image at all (corrupt/unreadable file) — a hard failure, never a
    silently-produced broken thumbnail.
    """
    src = Path(src)
    dest = Path(dest)
    try:
        with Image.open(src) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            size = img.size

            def _save(tmp_path: Path) -> None:
                img.save(tmp_path, format="JPEG", quality=85)

            atomic_write(dest, _save)
    except Exception as exc:
        raise AssetError(f"could not read photo {src}: {exc}") from exc
    return size


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def generate_video_poster(
    src: str | Path, dest: str | Path, seek: str = DEFAULT_POSTER_SEEK
) -> tuple[int, int]:
    """Extract a single early frame from a video as a JPEG poster, via ffmpeg.

    Written atomically (temp file + rename, see `atomic_write`) to `dest`.
    Returns the poster frame's `(width, height)`, read back from the
    generated JPEG via Pillow (which also double-checks ffmpeg actually
    produced a valid, readable image).

    Raises `AssetError`, identifying `src`, if: ffmpeg isn't installed; the
    ffmpeg subprocess fails (corrupt/unreadable video); or it exits
    successfully but the resulting file isn't a readable image. All of
    these are hard failures per Development Approach, never a silently
    broken/missing poster.
    """
    src = Path(src)
    dest = Path(dest)
    if not _ffmpeg_available():
        raise AssetError(
            f"ffmpeg is not installed; cannot generate a poster frame for {src}"
        )

    def _extract(tmp_path: Path) -> None:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                seek,
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssetError(
                f"ffmpeg failed to extract a poster frame from {src}: "
                f"{result.stderr.strip()}"
            )
        try:
            with Image.open(tmp_path) as img:
                img.load()
        except Exception as exc:
            raise AssetError(
                f"ffmpeg produced an unreadable poster frame for {src}: {exc}"
            ) from exc

    try:
        atomic_write(dest, _extract)
    except AssetError:
        raise
    except Exception as exc:
        raise AssetError(
            f"could not generate a poster frame for {src}: {exc}"
        ) from exc

    with Image.open(dest) as img:
        return img.size


def pick_cover_thumbnails(
    items: Sequence[ManifestItem], count: int = 3
) -> list[ManifestItem]:
    """Choose which items represent an album on the albums-index page.

    Picks `count` evenly-spaced items (including the first and last) from
    the *non-note* items in `items`' own order — notes have no thumbnail
    and are never eligible. If there are `count` or fewer eligible items,
    all of them are returned (in order). If there are none at all, returns
    an empty list (an album with only notes has no cover).
    """
    eligible = [item for item in items if not item.is_note]
    n = len(eligible)
    if count <= 0:
        return []
    if n <= count:
        return list(eligible)
    if count <= 1:
        return eligible[:1]

    step = (n - 1) / (count - 1)
    seen_indices: set[int] = set()
    result: list[ManifestItem] = []
    for i in range(count):
        idx = round(i * step)
        if idx not in seen_indices:
            seen_indices.add(idx)
            result.append(eligible[idx])
    return result


def cleanup_stale_thumbnails(
    album_dir: str | Path, current_filenames: Iterable[str]
) -> list[Path]:
    """Remove generated thumbnails/posters for items no longer published.

    `current_filenames` is the album's *current* set of source filenames
    (photos/videos only — notes have no generated thumbnail and aren't
    passed here). Any file directly inside `<album_dir>/.web/thumbs/` that
    doesn't map back to one of `current_filenames` via
    `thumbnail_filename` is removed, so re-running after `items` shrinks
    doesn't leave stale generated files behind in the real album folder
    forever.

    Returns the list of removed paths (empty if the thumbs directory
    doesn't exist yet, or nothing was stale).
    """
    directory = thumbs_dir(album_dir)
    if not directory.is_dir():
        return []

    keep = {thumbnail_filename(name) for name in current_filenames}
    removed: list[Path] = []
    for path in directory.iterdir():
        if path.is_file() and path.name not in keep:
            path.unlink()
            logger.info("removed stale %s", path)
            removed.append(path)
    return removed
