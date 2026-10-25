"""Jinja2 rendering of the album page, picture pages, and the site-wide albums index."""

from __future__ import annotations

import calendar
import json
import mimetypes
import random
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from prep.assets import THUMBS_DIRNAME, thumbnail_filename, thumbnail_path
from prep.atomic import atomic_write_text
from prep.layout import compute_grid_positions, group_by_day
from prep.manifest import AlbumInfo, ManifestItem, discover_albums
from prep.metadata import get_item_date, is_video


class RenderError(Exception):
    """Raised for `prep/render.py` hard-failures (e.g. sync_shared_static
    exhausting its swap-retry budget), following the same per-module
    exception-class pattern as ManifestError (prep/manifest.py) and
    AssetError (prep/assets.py) -- so `prep publish`'s CLI layer can catch
    it by name alongside those, without prep/render.py importing
    prep/cli.py's PublishError (which would create a circular import: cli.py
    already imports from render.py)."""

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

# Fixed subdirectory (relative to the site root) that the always-public
# cover-thumbnail copies live under, keyed by album path — see Solution
# Overview's "Index privacy tradeoff". Task 11 copies each album's chosen
# cover thumbnails here; this module only reads them back to build URLs.
COVERS_DIRNAME = ".covers"

# Relative-to-album-dir path of the per-album summary file consumed by
# render_albums_index (this task) and written by `prep publish`'s final
# rendering step (Task 11). Lives under .web/ alongside thumbnails, same
# convention as prep/assets.py's THUMBS_DIRNAME.
ALBUM_SUMMARY_FILENAME = ".web/album.json"


@dataclass
class AlbumSummary:
    """Per-album summary consumed by render_albums_index, written by `prep
    publish` (Task 11) to <album_dir>/.web/album.json via write_album_summary.

    ⚠️ SCHEMA CONTRACT between Task 10 (this module's reader) and Task 11
    (the only writer of real data): Task 11 MUST produce exactly this shape
    via write_album_summary — do not hand-roll the JSON or rename/restructure
    a field without updating both load_album_summary/write_album_summary and
    every caller. render_albums_index reads each album's .web/album.json
    instead of re-deriving dates/covers by re-scanning every album's full
    metadata on every `prep publish` run, so drift here silently breaks the
    index for every album, not just the one being published.

    Fields:
    - album_path: str — POSIX-style path (forward slashes, no leading/
      trailing slash) of the album folder relative to pictures_root, e.g.
      "2024/Family Reunion". Doubles as the album's URL path segment
      (Caddy's route path, per Task 12) and as the subdirectory name under
      <site_root>/.covers/<album_path>/.
    - title: str — the manifest's resolved title (already defaulted to the
      folder name by prep/manifest.py if publish.yaml omitted one).
    - public: bool — the manifest's `public` flag, verbatim.
    - earliest_date: str — ISO 8601 date (YYYY-MM-DD) of the album's
      earliest item's local date, used for year/month index grouping. Always
      a plain string on disk (JSON has no native date type) — parse with
      date.fromisoformat() when reading.
    - covers: list[str] — plain filenames (never a path, never containing
      "/") of the chosen cover thumbnails, already copied by Task 11 into
      the always-public <site_root>/.covers/<album_path>/ directory. The
      index must reference them from there, never from the album's own
      auth-protected .web/thumbs/ (see Solution Overview's "Index privacy
      tradeoff").
    """

    album_path: str
    title: str
    public: bool
    earliest_date: str
    covers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "album_path": self.album_path,
            "title": self.title,
            "public": self.public,
            "earliest_date": self.earliest_date,
            "covers": list(self.covers),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AlbumSummary":
        return cls(
            album_path=str(data["album_path"]),
            title=str(data["title"]),
            public=bool(data["public"]),
            earliest_date=str(data["earliest_date"]),
            covers=[str(c) for c in data.get("covers", [])],
        )


def load_album_summary(album_dir: str | Path) -> AlbumSummary | None:
    """Load <album_dir>/.web/album.json, or None if the album was never published.

    An album whose publish.yaml exists but has no album.json yet (never
    run through `prep publish`) is not an error here — the caller
    (render_albums_index) simply omits it from the index.
    """
    path = Path(album_dir) / ALBUM_SUMMARY_FILENAME
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return AlbumSummary.from_dict(data)


def write_album_summary(album_dir: str | Path, summary: AlbumSummary) -> Path:
    """Write `summary` to <album_dir>/.web/album.json, atomically.

    Task 11 is the real caller of this (with real title/public/date/cover
    data, assembled after asset generation picks the cover thumbnails and
    copies them into site/.covers/ — see the schema contract on
    AlbumSummary). This task's own tests call it directly with hand-built
    fixture summaries to exercise render_albums_index without Task 11
    existing yet.
    """
    dest = Path(album_dir) / ALBUM_SUMMARY_FILENAME
    content = json.dumps(summary.to_dict(), indent=2, sort_keys=True)
    atomic_write_text(dest, content)
    return dest


# Deterministic scatter ranges for each cover thumbnail's CSS-transform
# offset on the albums index (Task 10). Pixels for x/y, degrees for
# rotation — emitted into the template only as --offset-x/--offset-y/
# --rotate custom properties (never a literal CSS property value in a
# style attribute, per the Solution Overview's narrowed style-attribute
# rule), consumed by a fixed .cover-thumb rule in albums-index.css.
_COVER_OFFSET_RANGE_PX = (-12.0, 12.0)
_COVER_ROTATE_RANGE_DEG = (-8.0, 8.0)


def _cover_scatter_offset(album_path: str, index: int) -> dict:
    """Deterministic (offset_x, offset_y, rotate) for one album's cover thumbnail.

    Seeded from `album_path` plus the cover's index among that album's
    covers, using the same `random.Random(seed_string)` pattern
    prep/layout.py's compute_grid_positions already uses for its own
    deterministic randomization — so re-rendering an unchanged album
    produces identical offsets every time.
    """
    rng = random.Random(f"{album_path}:cover:{index}")
    return {
        "offset_x": round(rng.uniform(*_COVER_OFFSET_RANGE_PX), 1),
        "offset_y": round(rng.uniform(*_COVER_OFFSET_RANGE_PX), 1),
        "rotate": round(rng.uniform(*_COVER_ROTATE_RANGE_DEG), 1),
    }


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

        item_is_video = is_video(item.file)
        rows.append(
            {
                "kind": "video" if item_is_video else "photo",
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
    atomic_write_text(dest, html)
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

        item_is_video = is_video(item.file)
        mime_type, _ = mimetypes.guess_type(item.file)

        html = _env.get_template("picture_view.html").render(
            title=manifest.title,
            file=item.file,
            is_video=item_is_video,
            mime_type=mime_type,
            prev_href=picture_page_filename(prev_item.file) if prev_item else None,
            next_href=picture_page_filename(next_item.file) if next_item else None,
        )

        dest = picture_dir / filename
        atomic_write_text(dest, html)
        written.append(dest)

    if picture_dir.exists():
        for existing in picture_dir.iterdir():
            if existing.is_file() and existing.name not in expected_filenames:
                existing.unlink()

    return written


# No leading underscore -- tests import these directly (see
# tests/test_render.py), matching prep/cli.py's existing convention for its
# own swap-related constants (BACKUP_DIR_PREFIX/STAGING_DIR_PREFIX) rather
# than this module's previous underscore-prefixed-plus-module-qualified-
# access convention (code smells review finding #3's naming-consistency
# half).
STATIC_STAGING_PREFIX = ".static-staging-"
STATIC_BACKUP_PREFIX = ".static-backup-"
STATIC_SWAP_MAX_ATTEMPTS = 25

# Delay between retry attempts of the directory swap below -- named rather
# than an inline magic number, matching the project's habit of naming every
# tunable constant.
STATIC_SWAP_RETRY_DELAY_SECONDS = 0.01


@dataclass
class _StaticSwapAttempt:
    """Outcome of one _attempt_static_swap call.

    `backup`, when set, is a backup directory still sitting on disk that
    the caller must keep tracking (for eventual cleanup-on-success or
    restore-on-total-failure) -- None means either nothing needed
    preserving (dest didn't exist yet) or this attempt already restored
    it back onto `dest` itself as a stopgap, so the caller has nothing
    left to track for this attempt.
    """

    succeeded: bool
    backup: Path | None
    error: Exception | None


def _attempt_static_swap(staging: Path, dest: Path) -> _StaticSwapAttempt:
    """One attempt at atomically swapping `staging` into `dest`'s place
    (two renames: move any existing `dest` aside to a fresh backup
    directory, then move `staging` into `dest`'s place).

    Extracted out of sync_shared_static's retry loop (see code smells
    review finding #4) so the loop itself reads as a simple "try until
    succeeded or attempts exhausted" shell -- all the per-attempt
    concurrency reasoning (a concurrent sync_shared_static call racing the
    same two renames) lives here instead. See sync_shared_static's own
    docstring for why this needs to tolerate that concurrency at all.
    """
    backup = dest.parent / f"{STATIC_BACKUP_PREFIX}{uuid.uuid4().hex}"
    attempted_forward_swap = False
    try:
        try:
            dest.rename(backup)
        except FileNotFoundError:
            # No previous static/ to preserve: either this is the very
            # first sync, or a concurrent call already moved it aside.
            backup = None
        attempted_forward_swap = True
        staging.rename(dest)
        # `backup` (None if there was no previous dest to move aside, e.g.
        # the very first sync) now holds only the old, superseded static/
        # content -- still tracked here so the caller can clean it up once
        # `dest` is confirmed to hold the new content.
        return _StaticSwapAttempt(succeeded=True, backup=backup, error=None)
    except Exception as swap_exc:
        # Only the forward swap (staging.rename(dest)) actually failing can
        # mean a *different*, concurrent call's own forward swap landed
        # valid new content at `dest` in the meantime -- that's the one
        # case where stopping here without retrying is genuinely fine.
        # Check dest's validity right now, before touching `backup` at all
        # below: if we restored our own backup first and then checked,
        # "dest is valid" would always be true (we just put it there
        # ourselves) and would wrongly look identical to a concurrent
        # success, masking the fact that OUR intended new content (still
        # sitting untouched in `staging`) was never actually applied.
        resolved_by_other = False
        if attempted_forward_swap:
            try:
                resolved_by_other = dest.is_dir() and any(dest.iterdir())
            except OSError:
                # dest was renamed/removed by yet another concurrent call
                # in between the is_dir() and iterdir() calls above --
                # treat exactly like "not valid yet".
                resolved_by_other = False
        if resolved_by_other:
            return _StaticSwapAttempt(succeeded=True, backup=backup, error=None)
        if backup is not None and backup.exists() and not dest.exists():
            # Not resolved by anyone else. Put the previous static/ back
            # as a stopgap so the site is never left with none at all
            # while the caller retries -- but this is NOT success: our own
            # new content is still sitting in `staging`, untried or
            # unapplied, so the caller must retry the forward swap rather
            # than stopping here.
            try:
                backup.rename(dest)
                backup = None
            except Exception:
                pass
        return _StaticSwapAttempt(succeeded=False, backup=backup, error=swap_exc)


def sync_shared_static(site_root: str | Path) -> Path:
    """Copy prep/static/ into <site_root>/static/, replacing it via a single
    atomic directory rename rather than rmtree-then-copytree.

    Idempotent and cheap to repeat every `prep publish` run (per the plan's
    processing flow): always a full replacement of <site_root>/static/ with
    the package's current contents, never a merge and never symlinked, so
    a file removed from prep/static/ is also removed from the synced copy
    on the next run rather than lingering.

    `<site_root>/static/` is shared by every album's pages (unlike the
    per-album `.web/thumbs/`), and `_publish_album` only holds a per-album
    publish lock -- two different albums can legitimately call this
    function at roughly the same time. The previous rmtree-then-copytree
    implementation had a real window where `static/` didn't exist at all
    (site-wide CSS/JS 404s, not just for the albums being published), and
    two processes' rmtree/copytree calls could interleave and raise an
    uncaught FileExistsError/FileNotFoundError. Building the new tree in a
    staging directory and swapping it into place with os.rename (atomic on
    the same filesystem, which a temp dir under site_root always is)
    removes that window. A concurrent sync from another process winning
    the swap first is treated as benign (both processes copy the exact
    same package-versioned prep/static/ content, so whichever tree ends up
    in place is equivalent) rather than as an error.

    Returns the destination directory.
    """
    site_root = Path(site_root)
    site_root.mkdir(parents=True, exist_ok=True)
    dest = site_root / STATIC_DIRNAME

    # Deliberately NOT opportunistically sweeping leftover
    # `.static-staging-*`/`.static-backup-*` dirs here: under the exact
    # concurrency this function must tolerate (two different albums'
    # `prep publish` calling this at once), a sweep can't tell "orphaned
    # leftover from a crashed run" apart from "another process's staging/
    # backup dir that's actively in flight right now" and would delete
    # the latter out from under it. A crashed run's leftover is at worst
    # a small amount of disk space under site_root -- correctness never
    # depends on it being cleaned up, since every call below rebuilds
    # `dest` from scratch regardless.
    staging = Path(
        tempfile.mkdtemp(dir=str(site_root), prefix=STATIC_STAGING_PREFIX)
    )
    try:
        shutil.copytree(_STATIC_SRC, staging, dirs_exist_ok=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # The actual swap (two renames: move the old dest aside, move staging
    # into dest's place) is retried a bounded number of times rather than
    # classified by errno alone: a concurrent sync_shared_static call (a
    # different album's `prep publish` refreshing the same site-wide
    # static/ at once) can interleave its own two renames with ours in a
    # way that makes a single attempt fail with any of several OSError
    # variants (EEXIST, ENOTEMPTY, ENOENT depending on exactly where the
    # interleaving lands) without that alone telling us whether `dest`
    # ended up valid. Each attempt's own per-attempt concurrency reasoning
    # lives in _attempt_static_swap; this loop is just the "try until
    # succeeded or attempts exhausted" shell.
    #
    # Every backup dir created by ANY attempt (not just the last one) is
    # tracked in `pending_backups` and only disposed of once in a single
    # pass after the loop ends -- an earlier attempt's backup must not be
    # forgotten just because a later attempt is the one that ultimately
    # resolves `dest`, or it would leak on disk forever.
    pending_backups: list[Path] = []
    last_exc: Exception | None = None
    succeeded = False
    for _attempt in range(STATIC_SWAP_MAX_ATTEMPTS):
        outcome = _attempt_static_swap(staging, dest)
        if outcome.backup is not None:
            pending_backups.append(outcome.backup)
        if outcome.succeeded:
            succeeded = True
            break
        last_exc = outcome.error
        time.sleep(STATIC_SWAP_RETRY_DELAY_SECONDS)

    shutil.rmtree(staging, ignore_errors=True)

    if succeeded:
        # dest is confirmed valid -- every backup created along the way
        # (ours or restored-and-superseded) now holds only redundant,
        # equivalent content and is safe to discard.
        for stale_backup in pending_backups:
            shutil.rmtree(stale_backup, ignore_errors=True)
        return dest

    # Genuine failure: every attempt failed and dest never became valid
    # again. Try to restore the most recently rescued backup so the site
    # isn't left with no static/ at all, then raise a clear, specific
    # error naming what happened.
    restored = False
    for rescued in reversed(pending_backups):
        if rescued.exists() and not dest.exists():
            try:
                rescued.rename(dest)
                restored = True
                pending_backups.remove(rescued)
                break
            except Exception:
                continue
    for stale_backup in pending_backups:
        shutil.rmtree(stale_backup, ignore_errors=True)

    detail = (
        "the previous static/ has been restored so the site isn't left "
        "without one"
        if restored
        else f"{dest} is currently missing or empty"
    )
    raise RenderError(
        f"prep publish: failed to refresh shared static assets at {dest} "
        f"after {STATIC_SWAP_MAX_ATTEMPTS} attempts ({last_exc!r}); "
        f"{detail}. Investigate the underlying filesystem error (disk "
        f"full? permissions?) before publishing again."
    ) from last_exc


def _month_groups(albums: list[tuple[AlbumSummary, date]]) -> list[dict]:
    """Group (summary, earliest_date) pairs by year desc, then month Jan-Dec."""
    by_year_month: dict[tuple[int, int], list[tuple[AlbumSummary, date]]] = {}
    for summary, earliest in albums:
        by_year_month.setdefault((earliest.year, earliest.month), []).append(
            (summary, earliest)
        )

    years: dict[int, list[dict]] = {}
    for (year, month), entries in by_year_month.items():
        entries.sort(key=lambda pair: (pair[1], pair[0].album_path), reverse=True)
        years.setdefault(year, []).append(
            {
                "month": month,
                "label": calendar.month_name[month],
                "albums": entries,
            }
        )

    result = []
    for year in sorted(years, reverse=True):
        months = sorted(years[year], key=lambda m: m["month"])
        result.append({"year": year, "months": months})
    return result


def render_albums_index(site_root: str | Path, pictures_root: str | Path) -> Path:
    """Render the site-wide albums index to <site_root>/index.html.

    Reads each discovered album's <album_dir>/.web/album.json summary (see
    AlbumSummary's schema contract above) rather than re-deriving
    dates/covers by re-scanning every album's full metadata on every run.
    Only albums that have actually been published at least once — i.e.
    `load_album_summary` returns non-None — appear; a folder with a
    publish.yaml but no album.json yet (never published) is silently
    absent, not an error.

    Albums are grouped by year (newest first) then month (January-December
    within a year), both derived from each summary's `earliest_date`. Each
    tile's cover images are referenced from
    <site_root>/.covers/<album_path>/<filename> — the always-public copy
    (see Solution Overview's "Index privacy tradeoff") — never from the
    album's own auth-protected .web/thumbs/, regardless of whether the
    album is public or private.

    Returns the path written (`<site_root>/index.html`).
    """
    site_root = Path(site_root)
    pictures_root = Path(pictures_root)

    dated_summaries: list[tuple[AlbumSummary, date]] = []
    # populate_items=False: this function only reads each album's own
    # .web/album.json summary (title/public/earliest_date/covers) -- it
    # never touches album_info.manifest.items, so there's no need to pay
    # for auto-population's per-item EXIF/ffprobe date scan here (see
    # Quality review finding #4).
    for album_info in discover_albums(pictures_root, populate_items=False):
        summary = load_album_summary(album_info.path)
        if summary is None:
            continue
        dated_summaries.append((summary, date.fromisoformat(summary.earliest_date)))

    year_groups = _month_groups(dated_summaries)

    template_years = []
    for year_group in year_groups:
        template_months = []
        for month_group in year_group["months"]:
            template_albums = []
            for summary, _earliest in month_group["albums"]:
                encoded_path = quote(summary.album_path, safe="/")
                covers = [
                    {
                        "src": f"/{COVERS_DIRNAME}/{encoded_path}/{quote(filename)}",
                        **_cover_scatter_offset(summary.album_path, index),
                    }
                    for index, filename in enumerate(summary.covers)
                ]
                template_albums.append(
                    {
                        "title": summary.title,
                        "href": f"/{encoded_path}/",
                        "public": summary.public,
                        "covers": covers,
                    }
                )
            template_months.append(
                {"label": month_group["label"], "albums": template_albums}
            )
        template_years.append({"year": year_group["year"], "months": template_months})

    html = _env.get_template("albums_index.html").render(years=template_years)

    dest = site_root / "index.html"
    atomic_write_text(dest, html)
    return dest
