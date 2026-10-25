"""Shared atomic-write helper (temp file + rename), used by every module that
writes to a real album folder or other persisted config output.

The plan's Development Approach requires anything touching a real album
folder to be crash-safe (atomically rename into place, never leave a
half-written file behind) — this module is the one place that pattern is
implemented, so every writer (prep/assets.py's thumbnails/posters,
prep/render.py's HTML/JSON pages, prep/caddyconfig.py's Caddyfile,
prep/manifest.py's publish.yaml rewrites, prep/users.py's users.yml) shares
the exact same temp-file-plus-rename behavior instead of each reimplementing
a slightly different copy.

Also chmods the temp file to `mode` (world/group-readable by default)
before the rename: `tempfile.mkstemp` creates files at mode 0600, which is
fine for the temp file itself but would leave every *real* generated file
owner-only-readable -- a problem since `prep` always runs as root inside
its container (no non-root `USER` in prep/Dockerfile) while the host user
who needs to read/manage the generated tree is not root.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_MODE = 0o644


def atomic_write(
    dest: str | Path, write_fn: Callable[[Path], None], mode: int = DEFAULT_MODE
) -> None:
    """Call write_fn(tmp_path) to produce the file, then atomically rename it
    into dest.

    tmp_path is a real (empty, pre-created) file in dest's own directory,
    with the same suffix as dest, so tools that infer format from a file
    extension (e.g. ffmpeg) behave the same as they would writing dest
    directly. On any failure, the temp file is cleaned up and the exception
    propagates -- dest is never left partially written.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=dest.parent, prefix=f".{dest.name}.", suffix=dest.suffix or ".tmp"
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        write_fn(tmp_path)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
        # DEBUG, not INFO: this is the low-level temp-file-plus-rename
        # primitive, called for every write (including ones into a
        # transient staging directory later swapped into its real final
        # location, e.g. prep/cli.py's thumbnail generation) -- `dest`
        # here isn't always the file's eventual user-meaningful path.
        # Callers that write directly to a real final destination (most
        # of them) log their own INFO-level message naming that path;
        # this is just always-correct low-level plumbing for troubleshooting.
        logger.debug("atomic_write: wrote %s", dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(
    dest: str | Path, content: str, mode: int = DEFAULT_MODE
) -> None:
    """Write `content` to `dest` via temp-file-plus-rename (crash-safe)."""

    def _write(tmp_path: Path) -> None:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(content)

    atomic_write(dest, _write, mode=mode)
