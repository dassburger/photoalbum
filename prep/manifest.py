"""publish.yaml model: load/validate/save, and discover_albums() across the pictures root."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ruamel.yaml import YAML

from prep.atomic import atomic_write
from prep.metadata import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, get_item_date

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "publish.yaml"
WORK_DIRNAME = "work"

# Fixed whitelist of extensions auto-population scans for: exactly the
# photo/video extensions prep/metadata.py knows how to extract a capture
# date for (reused here rather than redefined, to avoid drift — see
# metadata.py's own note that this list doubles as the manifest whitelist).
# Explicitly excludes .CR2, .xmp, and anything else.
AUTO_POPULATE_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

_yaml = YAML()
_yaml.preserve_quotes = True


class ManifestError(Exception):
    """Raised for publish.yaml parsing/validation hard-failures."""


@dataclass
class ManifestItem:
    """A single =items= entry: either a file reference or a standalone note.

    Exactly one of `file`/`note` is set — enforced at construction so a
    malformed item (e.g. an items entry with neither key) fails loudly
    rather than silently producing a blank tile downstream.
    """

    file: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if (self.file is None) == (self.note is None):
            raise ManifestError(
                "each items entry must have exactly one of 'file' or 'note', "
                f"got file={self.file!r} note={self.note!r}"
            )

    @property
    def is_note(self) -> bool:
        return self.note is not None


@dataclass
class Manifest:
    """Parsed, in-memory representation of one album's publish.yaml.

    `items` is always in its final, authoritative order: when the manifest
    explicitly listed items, that order is preserved exactly as written
    (never re-sorted chronologically — see Task 5's day-grouping, which
    buckets this same order by date purely to place separators). When
    `items` was auto-populated here (the manifest omitted it), the order is
    chosen once, by date, at load time.
    """

    album_dir: Path
    title: str
    public: bool
    allowed_users: list[str] = field(default_factory=list)
    items: list[ManifestItem] = field(default_factory=list)


def _parse_item(raw) -> ManifestItem:
    file_val = raw.get("file") if hasattr(raw, "get") else None
    note_val = raw.get("note") if hasattr(raw, "get") else None
    return ManifestItem(
        file=str(file_val) if file_val is not None else None,
        note=str(note_val) if note_val is not None else None,
    )


def _auto_populate_items(album_dir: Path) -> list[ManifestItem]:
    """Scan album_dir (non-recursive, excluding work/) for whitelisted files.

    Sorted by prep/metadata.py's get_item_date (mtime-fallback included),
    with filename as a deterministic tiebreaker for same-instant dates.
    """
    candidates = []
    for path in sorted(album_dir.iterdir()):
        if path.name == WORK_DIRNAME or path.name == MANIFEST_FILENAME:
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in AUTO_POPULATE_EXTENSIONS:
            continue
        candidates.append(path)

    dated = [(path, get_item_date(path)[0]) for path in candidates]
    dated.sort(key=lambda pair: (pair[1], pair[0].name))
    return [ManifestItem(file=path.name) for path, _ in dated]


def load_manifest(album_dir: str | Path, *, populate_items: bool = True) -> Manifest:
    """Load and parse an album's publish.yaml.

    `title` defaults to the album folder's name when absent. `items`, when
    omitted from the manifest entirely, is auto-populated (see
    _auto_populate_items); when present, it's parsed and kept exactly in
    its written order.

    `populate_items` defaults to True (full behavior, required for anything
    that actually renders pages, e.g. `prep publish`). Pass False to skip
    the auto-population scan (and the per-item EXIF/ffprobe date read it
    requires) entirely when a manifest's items aren't needed at all --
    e.g. Caddyfile-route generation and the albums index only ever need
    `title`/`public`/`allowed_users`/`album_dir`, never `items` (see
    Quality review finding #4). An *explicit* `items` list is still parsed
    either way, since that's cheap (no date lookups) -- only the
    auto-population scan is skipped.

    This does not validate the manifest (see validate_manifest) — a
    nonexistent file reference or unknown user is parsed successfully here
    and only rejected by a later, explicit validate_manifest call.
    """
    album_dir = Path(album_dir)
    manifest_path = album_dir / MANIFEST_FILENAME
    if manifest_path.exists():
        with manifest_path.open("r") as f:
            data = _yaml.load(f)
        if data is None:
            data = {}
    else:
        data = {}

    title = data.get("title")
    title = str(title) if title is not None else album_dir.name
    public = bool(data.get("public", False))
    allowed_users = [str(u) for u in (data.get("allowed_users") or [])]

    raw_items = data.get("items")
    if raw_items is not None:
        items = [_parse_item(raw) for raw in raw_items]
    elif populate_items:
        items = _auto_populate_items(album_dir)
    else:
        items = []

    return Manifest(
        album_dir=album_dir,
        title=title,
        public=public,
        allowed_users=allowed_users,
        items=items,
    )


def validate_manifest(manifest: Manifest, users: Mapping[str, str]) -> None:
    """Hard-validate a loaded manifest. Raises ManifestError on any failure.

    Checks (all hard errors, per Development Approach's "fail loudly"
    rule):
    - every allowed_users entry is a known username in `users`
    - no explicit items file entry resolves *outside* the album's own
      directory (e.g. via a `../` path-traversal segment) -- such an entry
      could otherwise read/publish another album's (possibly private)
      content, since nothing else here or in prep/assets.py's
      thumbnail/cover-copy path confirms the file actually belongs to this
      album (see Quality review finding #1)
    - no explicit items file entry resolves inside the album's work/
      subdirectory (that content must never be published, regardless of
      whether it happens to exist) -- checked case-insensitively against
      every path component, matching Caddy's own case-insensitive `path`
      matcher for the generated route's work/ exclusion (prep/caddyconfig.py),
      so a case-variant directory like `Work/` is rejected here at publish
      time with a clear error instead of silently 404ing at serve time
      (see Implementation Correctness review finding #2)
    - every explicit items file entry exists on disk
    """
    unknown_users = [u for u in manifest.allowed_users if u not in users]
    if unknown_users:
        raise ManifestError(
            f"{manifest.album_dir}: unknown user(s) in allowed_users: "
            f"{', '.join(unknown_users)}"
        )

    album_dir_resolved = manifest.album_dir.resolve()
    for item in manifest.items:
        if item.is_note:
            continue
        file_path = (manifest.album_dir / item.file).resolve()

        if file_path != album_dir_resolved and album_dir_resolved not in file_path.parents:
            raise ManifestError(
                f"{manifest.album_dir}: item file escapes the album "
                f"directory and must not be published: {item.file}"
            )

        relative_parts = file_path.relative_to(album_dir_resolved).parts[:-1]
        if any(part.lower() == WORK_DIRNAME for part in relative_parts):
            raise ManifestError(
                f"{manifest.album_dir}: item file is inside {WORK_DIRNAME}/ "
                f"and must not be published: {item.file}"
            )
        if not file_path.exists():
            raise ManifestError(
                f"{manifest.album_dir}: item file does not exist: {item.file}"
            )


@dataclass
class AlbumInfo:
    """An album discovered under the pictures root: its folder + manifest."""

    path: Path
    manifest: Manifest


def remove_user_from_manifests(username: str, pictures_root: str | Path) -> list[Path]:
    """Strip `username` from every album's allowed_users list where present.

    Walks every publish.yaml under pictures_root (same "skip anything under
    work/" rule discover_albums uses), and for each one that lists
    `username` in allowed_users, removes it and saves the file back via
    ruamel.yaml round-trip mode (preserving comments/formatting/order of
    everything else). Albums that don't list `username` at all are left
    completely untouched (not even re-saved), so an unrelated album's
    formatting never churns.

    Returns the list of album directories actually touched, for `prep
    users delete` (task 13) to report back to the operator -- this is also
    why access revocation is immediate: an album whose private
    basic_auth-protected ACL no longer lists a deleted user has that
    reflected the next time the Caddyfile is regenerated (the same
    sync_caddy_routes() step `users delete` runs right after calling this).
    """
    pictures_root = Path(pictures_root)
    touched: list[Path] = []
    for manifest_path in sorted(pictures_root.rglob(MANIFEST_FILENAME)):
        relative_parts = manifest_path.relative_to(pictures_root).parts[:-1]
        if any(part.lower() == WORK_DIRNAME for part in relative_parts):
            continue

        with manifest_path.open("r") as f:
            data = _yaml.load(f)
        if data is None:
            continue

        allowed_users = data.get("allowed_users")
        if not allowed_users or username not in allowed_users:
            continue

        allowed_users.remove(username)

        def _save(tmp_path: Path, data=data) -> None:
            with tmp_path.open("w") as f:
                _yaml.dump(data, f)

        atomic_write(manifest_path, _save)
        logger.info("wrote %s (removed %r from allowed_users)", manifest_path, username)
        touched.append(manifest_path.parent)

    return touched


def discover_albums(
    pictures_root: str | Path, *, populate_items: bool = True
) -> list[AlbumInfo]:
    """Walk pictures_root for every publish.yaml, returning path+manifest pairs.

    Never descends into a work/ subdirectory (checked case-insensitively,
    matching Caddy's own case-insensitive `path` matcher -- see
    validate_manifest's docstring and Implementation Correctness review
    finding #2) — a publish.yaml accidentally left under one is not a real
    album and is skipped. Results are sorted by album path for
    deterministic ordering.

    `populate_items` is forwarded to `load_manifest` for each discovered
    album -- pass False when the caller only needs each album's
    path/title/public/allowed_users (e.g. Caddyfile-route generation, the
    albums index), to skip the potentially expensive per-item
    EXIF/ffprobe-based auto-population date scan (see Quality review
    finding #4).
    """
    pictures_root = Path(pictures_root)
    albums = []
    for manifest_path in sorted(pictures_root.rglob(MANIFEST_FILENAME)):
        relative_parts = manifest_path.relative_to(pictures_root).parts[:-1]
        if any(part.lower() == WORK_DIRNAME for part in relative_parts):
            continue
        album_dir = manifest_path.parent
        albums.append(
            AlbumInfo(
                path=album_dir,
                manifest=load_manifest(album_dir, populate_items=populate_items),
            )
        )
    return albums
