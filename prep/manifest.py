"""publish.yaml model: load/validate/save, and discover_albums() across the pictures root."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ruamel.yaml import YAML

from prep.metadata import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, get_item_date

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


def load_manifest(album_dir: str | Path) -> Manifest:
    """Load and parse an album's publish.yaml.

    `title` defaults to the album folder's name when absent. `items`, when
    omitted from the manifest entirely, is auto-populated (see
    _auto_populate_items); when present, it's parsed and kept exactly in
    its written order.

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
    else:
        items = _auto_populate_items(album_dir)

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
    - no explicit items file entry resolves inside the album's work/
      subdirectory (that content must never be published, regardless of
      whether it happens to exist)
    - every explicit items file entry exists on disk
    """
    unknown_users = [u for u in manifest.allowed_users if u not in users]
    if unknown_users:
        raise ManifestError(
            f"{manifest.album_dir}: unknown user(s) in allowed_users: "
            f"{', '.join(unknown_users)}"
        )

    work_dir = (manifest.album_dir / WORK_DIRNAME).resolve()
    for item in manifest.items:
        if item.is_note:
            continue
        file_path = (manifest.album_dir / item.file).resolve()
        if file_path == work_dir or work_dir in file_path.parents:
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


def discover_albums(pictures_root: str | Path) -> list[AlbumInfo]:
    """Walk pictures_root for every publish.yaml, returning path+manifest pairs.

    Never descends into a work/ subdirectory — a publish.yaml accidentally
    left under one is not a real album and is skipped. Results are sorted
    by album path for deterministic ordering.
    """
    pictures_root = Path(pictures_root)
    albums = []
    for manifest_path in sorted(pictures_root.rglob(MANIFEST_FILENAME)):
        relative_parts = manifest_path.relative_to(pictures_root).parts[:-1]
        if WORK_DIRNAME in relative_parts:
            continue
        album_dir = manifest_path.parent
        albums.append(AlbumInfo(path=album_dir, manifest=load_manifest(album_dir)))
    return albums
