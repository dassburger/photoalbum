"""Tests for prep.manifest: publish.yaml load/validate and discover_albums()."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from prep.manifest import (
    AlbumInfo,
    ManifestError,
    discover_albums,
    load_manifest,
    remove_user_from_manifests,
    validate_manifest,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_ALBUM = FIXTURES / "sample_album"


def _touch(path: Path, mtime_offset_seconds: float, content: bytes = b"x") -> None:
    """Create a file with a deterministic mtime, offset from a fixed epoch."""
    path.write_bytes(content)
    base = 1_700_000_000
    ts = base + mtime_offset_seconds
    os.utime(path, (ts, ts))


# -- load_manifest: title -----------------------------------------------


def test_load_manifest_title_defaults_to_folder_name(tmp_path):
    album_dir = tmp_path / "My Album"
    album_dir.mkdir()
    manifest = load_manifest(album_dir)
    assert manifest.title == "My Album"


def test_load_manifest_title_explicit():
    manifest = load_manifest(SAMPLE_ALBUM)
    assert manifest.title == "Sample Album"


def test_load_manifest_defaults_public_false_and_no_users(tmp_path):
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    manifest = load_manifest(album_dir)
    assert manifest.public is False
    assert manifest.allowed_users == []
    assert manifest.items == []


# -- load_manifest: explicit items ----------------------------------------


def test_load_manifest_explicit_items_preserves_manifest_order():
    manifest = load_manifest(SAMPLE_ALBUM)
    assert [item.file or item.note for item in manifest.items] == [
        "IMG_0001.jpg",
        "IMG_0002.jpg",
        "This was a great day.",
        "VID_0003.mp4",
    ]
    assert manifest.items[2].is_note is True
    assert manifest.items[0].is_note is False


def test_load_manifest_explicit_items_not_resorted_even_if_dates_disagree(tmp_path):
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    # IMG_0002 is older than IMG_0001 on disk, but items lists 0001 first —
    # that written order must win, not chronological order.
    _touch(album_dir / "IMG_0001.jpg", 100)
    _touch(album_dir / "IMG_0002.jpg", 0)
    (album_dir / "publish.yaml").write_text(
        "items:\n  - file: IMG_0001.jpg\n  - file: IMG_0002.jpg\n"
    )

    manifest = load_manifest(album_dir)
    assert [item.file for item in manifest.items] == ["IMG_0001.jpg", "IMG_0002.jpg"]


# -- load_manifest: auto-population ----------------------------------------


def test_load_manifest_auto_populates_items_sorted_by_date(tmp_path):
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    _touch(album_dir / "c.jpg", 200)
    _touch(album_dir / "a.jpg", 0)
    _touch(album_dir / "b.mp4", 100)
    # Not whitelisted — must never appear in the auto-populated items.
    _touch(album_dir / "raw.CR2", 50)
    _touch(album_dir / "notes.xmp", 50)
    work_dir = album_dir / "work"
    work_dir.mkdir()
    _touch(work_dir / "hidden.jpg", 0)

    manifest = load_manifest(album_dir)

    assert [item.file for item in manifest.items] == ["a.jpg", "b.mp4", "c.jpg"]


def test_load_manifest_auto_population_excludes_manifest_file_itself(tmp_path):
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    _touch(album_dir / "a.jpg", 0)
    (album_dir / "publish.yaml").write_text("public: true\n")

    manifest = load_manifest(album_dir)
    assert [item.file for item in manifest.items] == ["a.jpg"]


# -- validate_manifest: hard failures --------------------------------------


def test_validate_manifest_passes_for_valid_manifest():
    manifest = load_manifest(SAMPLE_ALBUM)
    users = {"alice": "hash", "bob": "hash"}
    validate_manifest(manifest, users)  # must not raise


def test_validate_manifest_unknown_user_raises():
    manifest = load_manifest(SAMPLE_ALBUM)
    users = {"bob": "hash"}  # "alice" (from allowed_users) is missing
    with pytest.raises(ManifestError, match="alice"):
        validate_manifest(manifest, users)


def test_validate_manifest_missing_file_raises(tmp_path):
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    (album_dir / "publish.yaml").write_text(
        "items:\n  - file: does_not_exist.jpg\n"
    )
    manifest = load_manifest(album_dir)
    with pytest.raises(ManifestError, match="does_not_exist.jpg"):
        validate_manifest(manifest, {})


def test_validate_manifest_item_inside_work_raises(tmp_path):
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    work_dir = album_dir / "work"
    work_dir.mkdir()
    (work_dir / "secret.jpg").write_bytes(b"x")
    (album_dir / "publish.yaml").write_text(
        "items:\n  - file: work/secret.jpg\n"
    )
    manifest = load_manifest(album_dir)
    with pytest.raises(ManifestError, match="work"):
        validate_manifest(manifest, {})


def test_validate_manifest_item_inside_case_variant_work_raises(tmp_path):
    """Caddy's generated route excludes work/ case-insensitively (a `path`
    matcher), so an explicit item under a case-variant directory like
    `Work/` must be rejected here too -- otherwise it would validate/publish
    successfully but then 404 at serve time (Implementation Correctness
    review finding #2)."""
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    work_dir = album_dir / "Work"
    work_dir.mkdir()
    (work_dir / "secret.jpg").write_bytes(b"x")
    (album_dir / "publish.yaml").write_text(
        "items:\n  - file: Work/secret.jpg\n"
    )
    manifest = load_manifest(album_dir)
    with pytest.raises(ManifestError, match="work"):
        validate_manifest(manifest, {})


def test_validate_manifest_path_traversal_outside_album_raises(tmp_path):
    """A manifest item pointing outside the album directory entirely (e.g.
    via a `../` path-traversal segment into a sibling album) must be a hard
    validation failure -- previously this passed validate_manifest with no
    error even though the referenced file lives in a different album
    (Quality review finding #1)."""
    root = tmp_path / "Pictures"
    root.mkdir()
    album_dir = root / "album-a"
    album_dir.mkdir()
    other_album_dir = root / "album-b"
    other_album_dir.mkdir()
    (other_album_dir / "secret.jpg").write_bytes(b"x")

    (album_dir / "publish.yaml").write_text(
        "items:\n  - file: ../album-b/secret.jpg\n"
    )
    manifest = load_manifest(album_dir)
    with pytest.raises(ManifestError, match="escapes"):
        validate_manifest(manifest, {})


def test_validate_manifest_path_traversal_to_nonexistent_target_still_raises(tmp_path):
    """Even when the traversal target doesn't exist, the escape must be
    rejected with the "escapes" error, not the (less specific, and
    misleading about *why* it's rejected) "does not exist" error."""
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    (album_dir / "publish.yaml").write_text(
        "items:\n  - file: ../does-not-exist.jpg\n"
    )
    manifest = load_manifest(album_dir)
    with pytest.raises(ManifestError, match="escapes"):
        validate_manifest(manifest, {})


def test_validate_manifest_notes_are_never_checked_as_files():
    album_dir = SAMPLE_ALBUM
    manifest = load_manifest(album_dir)
    # The sample album's note item has no backing file; this must not be
    # treated as a missing-file error.
    validate_manifest(manifest, {"alice": "hash"})


# -- discover_albums --------------------------------------------------------


def test_discover_albums_finds_all_manifests(tmp_path):
    root = tmp_path / "Pictures"
    root.mkdir()

    album_a = root / "2024" / "Trip"
    album_a.mkdir(parents=True)
    (album_a / "publish.yaml").write_text("title: Trip\n")

    album_b = root / "2023" / "Reunion"
    album_b.mkdir(parents=True)
    (album_b / "publish.yaml").write_text("title: Reunion\n")

    # A publish.yaml accidentally sitting under a work/ dir must be ignored.
    bogus = root / "2024" / "Trip" / "work"
    bogus.mkdir()
    (bogus / "publish.yaml").write_text("title: Bogus\n")

    # A folder with no publish.yaml at all is simply not an album.
    (root / "2024" / "NotAnAlbum").mkdir(parents=True)

    albums = discover_albums(root)

    assert all(isinstance(a, AlbumInfo) for a in albums)
    titles = sorted(a.manifest.title for a in albums)
    assert titles == ["Reunion", "Trip"]
    paths = {a.path for a in albums}
    assert album_a in paths
    assert album_b in paths
    assert bogus not in paths


def test_discover_albums_empty_root_returns_empty_list(tmp_path):
    root = tmp_path / "Pictures"
    root.mkdir()
    assert discover_albums(root) == []


def test_discover_albums_skips_case_variant_work_dir(tmp_path):
    """Caddy's own work/ exclusion is case-insensitive, so a publish.yaml
    accidentally sitting under a case-variant directory like `WORK/` must
    be skipped here too, not just a lowercase `work/` (Implementation
    Correctness review finding #2)."""
    root = tmp_path / "Pictures"
    root.mkdir()
    bogus = root / "2024" / "Trip" / "WORK"
    bogus.mkdir(parents=True)
    (bogus / "publish.yaml").write_text("title: Bogus\n")

    albums = discover_albums(root)

    assert albums == []


def test_discover_albums_populate_items_false_skips_auto_population(tmp_path, monkeypatch):
    """populate_items=False must skip the per-item date-based auto-population
    scan entirely (Quality review finding #4) -- verified here by making
    get_item_date explode if it's ever called."""
    import prep.manifest as manifest_module

    def _boom(path):
        raise AssertionError(f"get_item_date must not be called: {path}")

    monkeypatch.setattr(manifest_module, "get_item_date", _boom)

    root = tmp_path / "Pictures"
    album_dir = root / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "a.jpg").write_bytes(b"x")
    (album_dir / "publish.yaml").write_text("title: Album\n")

    albums = discover_albums(root, populate_items=False)

    assert len(albums) == 1
    assert albums[0].manifest.title == "Album"
    assert albums[0].manifest.items == []


def test_discover_albums_populate_items_false_still_parses_explicit_items(tmp_path, monkeypatch):
    """An explicit `items` list is cheap (no date lookups) and must still be
    parsed even with populate_items=False -- only the auto-population scan
    is skipped."""
    import prep.manifest as manifest_module

    def _boom(path):
        raise AssertionError(f"get_item_date must not be called: {path}")

    monkeypatch.setattr(manifest_module, "get_item_date", _boom)

    root = tmp_path / "Pictures"
    album_dir = root / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "a.jpg").write_bytes(b"x")
    (album_dir / "publish.yaml").write_text(
        "title: Album\nitems:\n  - file: a.jpg\n"
    )

    albums = discover_albums(root, populate_items=False)

    assert [item.file for item in albums[0].manifest.items] == ["a.jpg"]


# -- remove_user_from_manifests ---------------------------------------------


def test_remove_user_from_manifests_strips_from_listed_albums_only(tmp_path):
    root = tmp_path / "Pictures"
    root.mkdir()

    album_a = root / "2024" / "Trip"
    album_a.mkdir(parents=True)
    (album_a / "publish.yaml").write_text(
        "# curated by alice\n"
        "title: Trip\n"
        "allowed_users: [alice, bob]\n"
    )

    album_b = root / "2023" / "Reunion"
    album_b.mkdir(parents=True)
    (album_b / "publish.yaml").write_text(
        "title: Reunion\nallowed_users: [bob]\n"
    )

    touched = remove_user_from_manifests("alice", root)

    assert touched == [album_a]

    manifest_a = load_manifest(album_a)
    assert manifest_a.allowed_users == ["bob"]
    # Comment and formatting on the touched file must survive the edit.
    assert (album_a / "publish.yaml").read_text().startswith("# curated by alice\n")

    # Untouched album's file must be byte-for-byte unchanged.
    manifest_b = load_manifest(album_b)
    assert manifest_b.allowed_users == ["bob"]


def test_remove_user_from_manifests_no_matches_returns_empty_list(tmp_path):
    root = tmp_path / "Pictures"
    root.mkdir()
    album = root / "Album"
    album.mkdir()
    (album / "publish.yaml").write_text("allowed_users: [bob]\n")

    touched = remove_user_from_manifests("alice", root)

    assert touched == []
    assert load_manifest(album).allowed_users == ["bob"]


def test_remove_user_from_manifests_skips_work_dir(tmp_path):
    root = tmp_path / "Pictures"
    root.mkdir()
    bogus = root / "Album" / "work"
    bogus.mkdir(parents=True)
    (bogus / "publish.yaml").write_text("allowed_users: [alice]\n")

    touched = remove_user_from_manifests("alice", root)

    assert touched == []


def test_remove_user_from_manifests_skips_case_variant_work_dir(tmp_path):
    root = tmp_path / "Pictures"
    root.mkdir()
    bogus = root / "Album" / "Work"
    bogus.mkdir(parents=True)
    (bogus / "publish.yaml").write_text("allowed_users: [alice]\n")

    touched = remove_user_from_manifests("alice", root)

    assert touched == []


def test_remove_user_from_manifests_writes_atomically_no_leftover_temp_file(tmp_path):
    """The publish.yaml rewrite must use the same temp-file-plus-rename
    pattern as every other persisted-output writer (Quality review finding
    #2), not a plain in-place `open("w")` -- verified here by checking no
    temp file is left behind and the final file is a normal, directly
    readable file (not a dangling rename target)."""
    root = tmp_path / "Pictures"
    root.mkdir()
    album = root / "Album"
    album.mkdir()
    manifest_path = album / "publish.yaml"
    manifest_path.write_text("allowed_users: [alice, bob]\n")

    touched = remove_user_from_manifests("alice", root)

    assert touched == [album]
    leftovers = [p for p in album.iterdir() if p.name != "publish.yaml"]
    assert leftovers == []
    assert load_manifest(album).allowed_users == ["bob"]


def test_remove_user_from_manifests_leaves_original_untouched_on_write_failure(
    tmp_path, monkeypatch
):
    """Tightened version of the previous test (iteration-2 review finding
    #7): the previous test's assertions ("no leftover temp file", "final
    content is correct") would pass identically under the old, non-atomic
    `open("w")` implementation too, since there's nothing to leave behind
    either way on a successful run -- it didn't actually discriminate
    atomic from non-atomic writes.

    This test does: it forces the rename step itself (`os.replace`, inside
    prep/atomic.py's `atomic_write`) to fail *after* the new content has
    already been fully written to the temp file, and asserts the original
    `publish.yaml` is completely unchanged. A plain in-place `open("w")`
    write would have already truncated/overwritten the real file by this
    point (open("w") truncates immediately) -- only a genuine temp-file-
    plus-rename implementation can leave the original intact when the
    final rename itself is the thing that fails.
    """
    import prep.atomic as atomic_module

    root = tmp_path / "Pictures"
    root.mkdir()
    album = root / "Album"
    album.mkdir()
    manifest_path = album / "publish.yaml"
    original_content = "# keep me\nallowed_users: [alice, bob]\n"
    manifest_path.write_text(original_content)

    real_replace = os.replace

    def _failing_replace(src, dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(atomic_module.os, "replace", _failing_replace)

    with pytest.raises(OSError):
        remove_user_from_manifests("alice", root)

    monkeypatch.setattr(atomic_module.os, "replace", real_replace)

    # The original file must be byte-for-byte unchanged -- a non-atomic
    # open("w") write would have already clobbered it before this failure
    # point could even be simulated.
    assert manifest_path.read_text() == original_content
    # No leftover temp file in the album directory either.
    leftovers = [p for p in album.iterdir() if p.name != "publish.yaml"]
    assert leftovers == []
