"""Jinja2 rendering of the album page, picture pages, and the site-wide albums index."""

from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from prep.assets import THUMBS_DIRNAME, thumbnail_filename, thumbnail_path
from prep.layout import compute_grid_positions, group_by_day
from prep.manifest import AlbumInfo, ManifestItem
from prep.metadata import VIDEO_EXTENSIONS, get_item_date

# Fixed subdirectory (relative to the site root) that shared static assets
# are synced into. Pages reference these via the absolute "/static/..."
# URL path, matching the single unauthenticated Caddy `handle` for
# `/static/*` rooted at the site root (see Task 12).
STATIC_DIRNAME = "static"

# prep/static/ — the package's own copy, versioned with the code (not the
# photo archive).
_STATIC_SRC = Path(__file__).parent / "static"

# prep/templates/ — loaded once, module-level, since templates don't change
# between renders within a single `prep publish` run.
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def picture_page_filename(source_filename: str) -> str:
    """Return the generated picture-page filename for a source file.

    Named from the source file's *stem* (its extension dropped), e.g.
    `IMG_0001.jpg` -> `IMG_0001.html` — never from its position in `items`,
    so inserting/removing an item elsewhere in the manifest never renames
    another item's page/bookmark (see Task 9). Defined here, once, so the
    album page's links (Task 8) and the actual picture pages Task 9 writes
    always agree on the exact same name.
    """
    return f"{Path(source_filename).stem}.html"


def _atomic_write_text(dest: Path, content: str) -> None:
    """Write `content` to `dest` via temp-file-plus-rename (crash-safe)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _grid_rows_for_template(entries) -> list[dict]:
    """Flatten layout.py's GridEntry list into plain template-ready dicts.

    Each row carries only what album_page.html needs to render it — never
    a literal CSS property, only the `row_span`/`col_span` numbers the
    template emits as `--row-span`/`--col-span` custom properties (per the
    Solution Overview's narrowed style-attribute rule).
    """
    rows: list[dict] = []
    for entry in entries:
        if entry.kind == "separator":
            rows.append({"kind": "separator", "day": entry.day})
            continue

        item = entry.item
        if item.is_note:
            rows.append(
                {
                    "kind": "note",
                    "note": item.note,
                    "row_span": entry.row_span,
                    "col_span": entry.col_span,
                }
            )
            continue

        is_video = Path(item.file).suffix.lower() in VIDEO_EXTENSIONS
        rows.append(
            {
                "kind": "video" if is_video else "photo",
                "file": item.file,
                # Relative to the generated index.html, which lives
                # directly in the album folder alongside .web/thumbs/.
                "thumb_src": f"{THUMBS_DIRNAME}/{thumbnail_filename(item.file)}",
                "href": f"picture/{picture_page_filename(item.file)}",
                "row_span": entry.row_span,
                "col_span": entry.col_span,
            }
        )
    return rows


def render_album_page(album_info: AlbumInfo, album_dir: str | Path) -> Path:
    """Render the album page directly to <album_dir>/index.html.

    Wires together the manifest's authoritative item order and per-item
    dates (prep/metadata.get_item_date) into prep/layout.group_by_day for
    day-separator placement, reads back each item's already-generated
    thumbnail/poster dimensions from `.web/thumbs/` (produced earlier in
    the processing flow by prep/assets.py — see the plan's Processing
    Flow, asset generation runs before rendering) to feed
    prep/layout.compute_grid_positions aspect-ratio-aware spans, and renders
    prep/templates/album_page.html with the result.

    An item whose thumbnail hasn't been generated yet (e.g. calling this
    function directly without running asset generation first) simply falls
    back to layout.py's own default "square" aspect ratio rather than
    erroring — thumbnail generation failures are Task 6/11's concern, not
    this function's.

    Returns the path written (`<album_dir>/index.html`).
    """
    album_dir = Path(album_dir)
    manifest = album_info.manifest

    items_with_dates: list[tuple[ManifestItem, date | datetime | None]] = []
    for item in manifest.items:
        if item.is_note:
            items_with_dates.append((item, None))
        else:
            item_date, _ = get_item_date(album_dir / item.file)
            items_with_dates.append((item, item_date))

    buckets = group_by_day(items_with_dates)

    aspect_ratios: dict[str, float] = {}
    for item in manifest.items:
        if item.is_note:
            continue
        thumb_path = thumbnail_path(album_dir, item.file)
        if thumb_path.exists():
            with Image.open(thumb_path) as img:
                width, height = img.size
            if height:
                aspect_ratios[item.file] = width / height

    entries = compute_grid_positions(buckets, aspect_ratios, album_path=str(album_dir))
    rows = _grid_rows_for_template(entries)

    html = _env.get_template("album_page.html").render(title=manifest.title, rows=rows)

    dest = album_dir / "index.html"
    _atomic_write_text(dest, html)
    return dest


def render_picture_pages(album_info: AlbumInfo, album_dir: str | Path) -> list[Path]:
    """Render one picture-view page per non-note item into <album_dir>/picture/.

    Each page is named from its *source filename's* stem via
    `picture_page_filename` (the exact same helper Task 8's album page uses
    to build its links), never from its position in `items` — so inserting
    or removing an item elsewhere in the manifest never renames another
    item's page/bookmark.

    Notes have no picture page (they're inline-only on the album page) and
    are excluded entirely before computing prev/next, so a note sitting
    between two photos never breaks their adjacency.

    Also removes any `picture/<name>.html` left over from an item no
    longer present in `items` (re-run after `items` shrinks), so stale
    generated pages never linger in the real album folder.

    Returns the list of paths written (not including any removed stale
    pages).
    """
    album_dir = Path(album_dir)
    manifest = album_info.manifest
    picture_dir = album_dir / "picture"

    media_items = [item for item in manifest.items if not item.is_note]

    written: list[Path] = []
    expected_filenames: set[str] = set()
    for index, item in enumerate(media_items):
        filename = picture_page_filename(item.file)
        expected_filenames.add(filename)

        prev_item = media_items[index - 1] if index > 0 else None
        next_item = media_items[index + 1] if index + 1 < len(media_items) else None

        is_video = Path(item.file).suffix.lower() in VIDEO_EXTENSIONS
        mime_type, _ = mimetypes.guess_type(item.file)

        html = _env.get_template("picture_view.html").render(
            title=manifest.title,
            file=item.file,
            is_video=is_video,
            mime_type=mime_type,
            prev_href=picture_page_filename(prev_item.file) if prev_item else None,
            next_href=picture_page_filename(next_item.file) if next_item else None,
        )

        dest = picture_dir / filename
        _atomic_write_text(dest, html)
        written.append(dest)

    if picture_dir.exists():
        for existing in picture_dir.iterdir():
            if existing.is_file() and existing.name not in expected_filenames:
                existing.unlink()

    return written


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
