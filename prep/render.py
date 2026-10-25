"""Jinja2 rendering of the album page, picture pages, and the site-wide albums index."""

from __future__ import annotations

import shutil
from pathlib import Path

# Fixed subdirectory (relative to the site root) that shared static assets
# are synced into. Pages reference these via the absolute "/static/..."
# URL path, matching the single unauthenticated Caddy `handle` for
# `/static/*` rooted at the site root (see Task 12).
STATIC_DIRNAME = "static"

# prep/static/ — the package's own copy, versioned with the code (not the
# photo archive).
_STATIC_SRC = Path(__file__).parent / "static"


def sync_shared_static(site_root: str | Path) -> Path:
    """Copy prep/static/ into <site_root>/static/, overwriting it in place.

    Idempotent and cheap to repeat every `prep publish` run (per the plan's
    processing flow): always a full overwrite of <site_root>/static/ with
    the package's current contents, never a merge and never symlinked, so
    a file removed from prep/static/ is also removed from the synced copy
    on the next run rather than lingering.

    Returns the destination directory.
    """
    site_root = Path(site_root)
    dest = site_root / STATIC_DIRNAME
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_STATIC_SRC, dest)
    return dest
