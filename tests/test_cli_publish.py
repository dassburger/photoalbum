"""End-to-end tests for `prep publish` orchestration (prep/cli.py).

Exercises the full pipeline against a small fixture album directory built
in a temp dir (a `work/` subdir, a stray unlisted JPEG, and a note in
`items`), and the hard-validation-abort path (unknown user). A video-item
end-to-end test is also included, following the same pytest.skip-if-ffmpeg-
missing pattern already used in tests/test_assets.py/tests/test_metadata.py
-- it skips gracefully on the host (this sandbox has no ffmpeg/ffprobe
installed) and runs for real inside Task 16's container, which installs
ffmpeg (Test Coverage review finding #5).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

import prep.cli as cli_module
from prep.cli import cli
from prep.render import COVERS_DIRNAME, STATIC_DIRNAME

FIXTURES = Path(__file__).parent / "fixtures"


def _build_album(pictures_root: Path, relpath: str = "2024/Test Album") -> Path:
    """Build a small fixture album under pictures_root, matching the plan's
    Task 11 e2e test requirements: a work/ subdir, a stray unlisted JPEG, and
    a note interspersed in items."""
    album_dir = pictures_root / relpath
    album_dir.mkdir(parents=True)

    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    shutil.copy(FIXTURES / "sample_no_exif.jpg", album_dir / "IMG_0002.jpg")

    # Stray file present on disk but never listed in items — must never be
    # linked from any generated page.
    shutil.copy(FIXTURES / "sample_no_exif.jpg", album_dir / "IMG_9999_stray.jpg")

    # work/ subdir — must never be referenced either.
    work_dir = album_dir / "work"
    work_dir.mkdir()
    (work_dir / "IMG_0001.CR2").write_bytes(b"not a real raw file")

    (album_dir / "publish.yaml").write_text(
        "title: \"Test Album\"\n"
        "public: true\n"
        "items:\n"
        "  - file: IMG_0001.jpg\n"
        "  - note: \"This was the day it rained all afternoon.\"\n"
        "  - file: IMG_0002.jpg\n"
    )
    return album_dir


def _invoke_publish(
    monkeypatch,
    album_dir: Path,
    pictures_root: Path,
    site_root: Path,
    users_path: Path,
):
    """Invoke `prep publish`, with --caddy-dir pointed at a throwaway tmp
    dir (so the task-12 Caddyfile regen+reload step at the end of publish
    never touches the real repo's caddy/ directory) and reload_caddy()
    stubbed out -- this suite has no real Caddy container, and per the
    plan, tests must mock/stub reload_caddy() rather than exercise a real
    subprocess/docker call.
    """
    monkeypatch.setattr(cli_module, "reload_caddy", lambda *a, **kw: "reloaded Caddy (stub)")
    runner = CliRunner()
    return runner.invoke(
        cli,
        [
            "publish",
            str(album_dir),
            "--pictures-root",
            str(pictures_root),
            "--site-root",
            str(site_root),
            "--caddy-dir",
            str(pictures_root.parent / "caddy"),
            "--users-file",
            str(users_path),
        ],
    )


def test_publish_end_to_end(tmp_path, monkeypatch):
    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir()
    album_dir = _build_album(pictures_root)

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"

    result = _invoke_publish(monkeypatch, album_dir, pictures_root, site_root, users_path)
    assert result.exit_code == 0, result.output

    # -- per-album generated files, written directly into the real album dir
    thumbs_dir = album_dir / ".web" / "thumbs"
    assert (thumbs_dir / "IMG_0001.jpg.jpg").exists()
    assert (thumbs_dir / "IMG_0002.jpg.jpg").exists()
    # the stray, unlisted JPEG must never get a thumbnail generated for it
    assert not (thumbs_dir / "IMG_9999_stray.jpg.jpg").exists()

    index_html = (album_dir / "index.html").read_text()
    assert "IMG_0001.jpg" in index_html
    assert "IMG_0002.jpg" in index_html
    assert "rained all afternoon" in index_html

    assert (album_dir / "picture" / "IMG_0001.html").exists()
    assert (album_dir / "picture" / "IMG_0002.html").exists()

    summary_path = album_dir / ".web" / "album.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["album_path"] == "2024/Test Album"
    assert summary["public"] is True
    assert summary["covers"]

    # -- generated pages never link to work/ or the stray unlisted JPEG
    picture_html = (album_dir / "picture" / "IMG_0001.html").read_text()
    for html in (index_html, picture_html):
        assert "work/" not in html
        assert "IMG_9999_stray" not in html

    # -- site-wide output
    assert (site_root / STATIC_DIRNAME).is_dir()
    covers_dir = site_root / COVERS_DIRNAME / "2024" / "Test Album"
    assert covers_dir.is_dir()
    assert any(covers_dir.iterdir())

    site_index_html = (site_root / "index.html").read_text()
    assert "Test Album" in site_index_html


def test_publish_unknown_user_aborts_cleanly(tmp_path, monkeypatch):
    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir()
    album_dir = pictures_root / "2024" / "Bad Album"
    album_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    (album_dir / "publish.yaml").write_text(
        "public: false\n"
        "allowed_users: [nobody]\n"
        "items:\n"
        "  - file: IMG_0001.jpg\n"
    )

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"  # no users at all

    result = _invoke_publish(monkeypatch, album_dir, pictures_root, site_root, users_path)

    assert result.exit_code != 0
    assert "nobody" in result.output

    # nothing written for this album, and no site output either
    assert not (album_dir / ".web").exists()
    assert not (album_dir / "index.html").exists()
    assert not (album_dir / "picture").exists()
    assert not site_root.exists()


def test_publish_corrupt_image_aborts_without_partial_thumbnails(tmp_path, monkeypatch):
    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir()
    album_dir = pictures_root / "2024" / "Corrupt Album"
    album_dir.mkdir(parents=True)

    # First item is a perfectly good photo; second is corrupt/unreadable.
    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    shutil.copy(FIXTURES / "sample_corrupt.jpg", album_dir / "IMG_0002.jpg")

    (album_dir / "publish.yaml").write_text(
        "public: true\n"
        "items:\n"
        "  - file: IMG_0001.jpg\n"
        "  - file: IMG_0002.jpg\n"
    )

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"

    result = _invoke_publish(monkeypatch, album_dir, pictures_root, site_root, users_path)

    assert result.exit_code != 0
    assert "IMG_0002.jpg" in result.output

    # The good item's thumbnail must not have been left behind even though
    # it was generated before the corrupt item failed — all-or-nothing.
    assert not (album_dir / ".web" / "thumbs").exists()
    assert not (album_dir / "index.html").exists()


def test_generate_assets_atomically_rolls_back_on_partial_rename_failure(
    tmp_path, monkeypatch
):
    """On a first-ever publish (no pre-existing .web/thumbs/), a failure
    during the final directory-level swap must leave .web/thumbs/ exactly
    as it was before this run -- i.e. still not existing at all, and the
    half-built staging dir discarded -- rather than appearing with some
    items present and others missing."""
    from prep.cli import ManifestItem, _generate_assets_atomically

    album_dir = tmp_path / "album"
    album_dir.mkdir()
    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    shutil.copy(FIXTURES / "sample_no_exif.jpg", album_dir / "IMG_0002.jpg")

    media_items = [ManifestItem(file="IMG_0001.jpg"), ManifestItem(file="IMG_0002.jpg")]

    real_rename = Path.rename

    def _failing_rename(self, target):
        # final_dir doesn't exist yet (first-ever publish), so the only
        # Path.rename call in the swap phase is staging -> final_dir.
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "rename", _failing_rename)

    with pytest.raises(OSError):
        _generate_assets_atomically(album_dir, media_items)

    monkeypatch.setattr(Path, "rename", real_rename)

    # .web/thumbs/ must be exactly as it was before this run: it didn't
    # exist before, so it must not exist now, and no stray staging/backup
    # directory should be left behind under .web/ either.
    thumbs_dir = album_dir / ".web" / "thumbs"
    assert not thumbs_dir.exists()
    web_dir = album_dir / ".web"
    assert list(web_dir.iterdir()) == []


def test_generate_assets_atomically_restores_preexisting_thumbnails_on_failure(
    tmp_path, monkeypatch
):
    """Regression test for iteration-2 review finding #2: a partial failure
    on a RE-publish (the normal steady-state for any album published more
    than once) must restore the previously-valid thumbnails that existed
    before this run, not just leave .web/thumbs/ merely non-corrupt.

    The prior "rollback" fix (unlink each already-renamed destination)
    only covered the first-ever-publish case, where there was nothing to
    lose. Per-file-rename-into-a-live-directory always overwrites whatever
    was already at the destination, so unlinking it on failure destroys
    the just-written file without restoring the old one -- a strictly
    worse outcome (total data loss) than the bug it was meant to fix. The
    real fix builds the whole new set in a staging dir and swaps it in as
    a single directory-level rename, with the previous directory restored
    on failure.
    """
    from prep.cli import ManifestItem, _generate_assets_atomically

    album_dir = tmp_path / "album"
    album_dir.mkdir()
    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    shutil.copy(FIXTURES / "sample_no_exif.jpg", album_dir / "IMG_0002.jpg")

    media_items = [ManifestItem(file="IMG_0001.jpg"), ManifestItem(file="IMG_0002.jpg")]

    # Simulate "already published once": run it successfully first, so
    # .web/thumbs/ holds valid, previously-generated thumbnails.
    _generate_assets_atomically(album_dir, media_items)
    thumbs_dir = album_dir / ".web" / "thumbs"
    old_item1_bytes = (thumbs_dir / "IMG_0001.jpg.jpg").read_bytes()
    old_item2_bytes = (thumbs_dir / "IMG_0002.jpg.jpg").read_bytes()
    assert old_item1_bytes and old_item2_bytes

    # Now re-run (e.g. the curator reordered/retouched something) but make
    # the directory-level swap itself fail partway through, after the new
    # staging set has already been fully built.
    real_rename = Path.rename
    call_count = {"n": 0}

    def _flaky_rename(self, target):
        call_count["n"] += 1
        # First call during the swap phase is final_dir -> backup_dir
        # (must succeed so there's something to restore); second call is
        # staging -> final_dir (fails here).
        if call_count["n"] == 2:
            raise OSError("simulated rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _flaky_rename)

    with pytest.raises(OSError):
        _generate_assets_atomically(album_dir, media_items)

    # The pre-existing thumbnails from the earlier successful publish must
    # still be intact and byte-identical -- NOT deleted, NOT replaced with
    # a half-written new version.
    assert thumbs_dir.is_dir()
    assert (thumbs_dir / "IMG_0001.jpg.jpg").read_bytes() == old_item1_bytes
    assert (thumbs_dir / "IMG_0002.jpg.jpg").read_bytes() == old_item2_bytes

    # No leftover staging/backup directories should remain in .web/.
    web_dir = album_dir / ".web"
    leftover = [
        p for p in web_dir.iterdir() if p.name not in {"thumbs"}
    ]
    assert leftover == []


def test_generate_assets_atomically_preserves_backup_when_restore_also_fails(
    tmp_path, monkeypatch
):
    """Regression test for iteration-3 review finding #1 (critical): if
    the forward swap fails AND the automatic restore-back rename also
    fails (plausible -- same root cause, e.g. disk full, can affect both
    renames), the old `finally` block used to unconditionally delete
    `backup_dir` -- the one remaining intact copy of the album's old
    thumbnails -- causing total, permanent data loss. The fix must leave
    `backup_dir` on disk, with the pre-run thumbnail bytes intact, and
    raise a clear error naming its exact path instead."""
    from prep.cli import BACKUP_DIR_PREFIX, ManifestItem, _generate_assets_atomically

    album_dir = tmp_path / "album"
    album_dir.mkdir()
    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    shutil.copy(FIXTURES / "sample_no_exif.jpg", album_dir / "IMG_0002.jpg")

    media_items = [ManifestItem(file="IMG_0001.jpg"), ManifestItem(file="IMG_0002.jpg")]

    # Simulate "already published once".
    _generate_assets_atomically(album_dir, media_items)
    thumbs_dir = album_dir / ".web" / "thumbs"
    old_item1_bytes = (thumbs_dir / "IMG_0001.jpg.jpg").read_bytes()
    old_item2_bytes = (thumbs_dir / "IMG_0002.jpg.jpg").read_bytes()

    real_rename = Path.rename
    call_count = {"n": 0}

    def _doubly_flaky_rename(self, target):
        call_count["n"] += 1
        # Call 1: final_dir -> backup_dir -- let it succeed, so there's
        # something to restore.
        # Call 2: staging -> final_dir (the forward swap) -- fails.
        # Call 3: backup_dir -> final_dir (the restore-back attempt) --
        # ALSO fails, the scenario the prior rounds' fix never covered.
        if call_count["n"] in (2, 3):
            raise OSError("simulated rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _doubly_flaky_rename)

    with pytest.raises(Exception) as excinfo:
        _generate_assets_atomically(album_dir, media_items)

    monkeypatch.setattr(Path, "rename", real_rename)

    message = str(excinfo.value)

    # backup_dir must still exist on disk, holding the pre-run bytes
    # intact -- NOT silently deleted by `finally`.
    web_dir = album_dir / ".web"
    backups = [p for p in web_dir.iterdir() if p.name.startswith(BACKUP_DIR_PREFIX)]
    assert len(backups) == 1, f"expected exactly one surviving backup dir, got {backups}"
    backup_dir = backups[0]
    assert (backup_dir / "IMG_0001.jpg.jpg").read_bytes() == old_item1_bytes
    assert (backup_dir / "IMG_0002.jpg.jpg").read_bytes() == old_item2_bytes

    # A clear, specific error naming this exact surviving backup dir's own
    # path (not just "backup" in general, and not merely album_dir, which
    # would also match plenty of unrelated/vaguer error messages) so an
    # operator can recover manually.
    assert str(backup_dir) in message


def test_generate_assets_atomically_self_heals_orphaned_backup_when_final_dir_missing(
    tmp_path, monkeypatch
):
    """Regression test for iteration-3 review finding #2: if a previous
    run was killed between the two swap renames (final_dir missing, one
    orphaned `.thumbs-backup-*` dir left holding the last valid
    thumbnails), the next call must self-heal -- restore that backup into
    final_dir's place -- before doing any of its own work, rather than
    leaving the orphan to leak disk space forever with the album 404ing
    on thumbnails in the meantime."""
    from prep.cli import BACKUP_DIR_PREFIX, ManifestItem, _generate_assets_atomically

    album_dir = tmp_path / "album"
    album_dir.mkdir()
    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    media_items = [ManifestItem(file="IMG_0001.jpg")]

    # Publish once successfully, so final_dir holds valid thumbnails.
    _generate_assets_atomically(album_dir, media_items)
    thumbs_dir = album_dir / ".web" / "thumbs"
    old_bytes = (thumbs_dir / "IMG_0001.jpg.jpg").read_bytes()

    # Simulate "process was killed mid-swap": this is exactly the on-disk
    # state right after `final_dir.rename(backup_dir)` succeeded but
    # before `staging.rename(final_dir)` ran -- final_dir missing, an
    # orphaned backup dir sitting in its place.
    web_dir = album_dir / ".web"
    orphaned_backup = web_dir / f"{BACKUP_DIR_PREFIX}deadbeef"
    thumbs_dir.rename(orphaned_backup)
    assert not thumbs_dir.exists()

    # Make this run's OWN generation fail immediately, so the self-heal's
    # effect can be observed in isolation from whether this run's new
    # thumbnails ever get swapped in (self-heal runs first, at the very
    # top of the function, before any new work).
    import prep.cli as cli_module

    def _boom(*a, **kw):
        raise RuntimeError("simulated failure, unrelated to the self-heal")

    monkeypatch.setattr(cli_module, "generate_photo_thumbnail", _boom)

    with pytest.raises(RuntimeError):
        _generate_assets_atomically(album_dir, media_items)

    assert thumbs_dir.is_dir()
    assert (thumbs_dir / "IMG_0001.jpg.jpg").read_bytes() == old_bytes
    assert not orphaned_backup.exists()


def test_generate_assets_atomically_cleans_up_stale_orphaned_staging_dir(
    tmp_path,
):
    """Regression test for iteration-3 review finding #3: a stale
    `.thumbs-staging-*` dir left behind by a run that crashed during
    generation (before any rename happened) must be cleaned up by the
    next run's self-heal when final_dir is otherwise healthy, rather than
    accumulating forever as a disk-space leak."""
    from prep.cli import STAGING_DIR_PREFIX, ManifestItem, _generate_assets_atomically

    album_dir = tmp_path / "album"
    album_dir.mkdir()
    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    media_items = [ManifestItem(file="IMG_0001.jpg")]

    _generate_assets_atomically(album_dir, media_items)
    web_dir = album_dir / ".web"

    stale_staging = web_dir / f"{STAGING_DIR_PREFIX}leftover"
    stale_staging.mkdir()
    (stale_staging / "junk.jpg.jpg").write_bytes(b"half-written garbage")

    # final_dir exists normally here -- this stale staging dir is simply
    # a leftover from an unrelated crashed run, safe to discard outright.
    _generate_assets_atomically(album_dir, media_items)

    assert not stale_staging.exists()


def test_album_publish_lock_raises_clear_error_when_already_held(tmp_path):
    """Regression test for iteration-3 review finding #4: a second
    attempt to acquire the per-album publish lock while it's already
    held (e.g. a concurrent `prep publish` of the same album) must raise
    a clear, specific error rather than silently proceeding into the
    directory-swap race that can trigger finding #1's failure mode."""
    import fcntl
    import os

    from prep.cli import PublishError, _album_publish_lock

    album_dir = tmp_path / "album"
    album_dir.mkdir()

    lock_path = album_dir / ".prep-publish.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(PublishError, match="already being published"):
            with _album_publish_lock(album_dir):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_publish_aborts_cleanly_when_album_lock_already_held(tmp_path, monkeypatch):
    """Regression test (iteration-4 review finding) proving
    `_album_publish_lock` is actually wired around the write-phases of
    `_publish_album` itself, not just that the lock primitive works in
    isolation (the existing
    test_album_publish_lock_raises_clear_error_when_already_held only
    exercises `_album_publish_lock` directly). Holding the album's lock
    externally -- simulating a concurrent `prep publish` of the SAME
    album already in flight -- must cause this publish attempt to raise
    the "already being published" error (surfaced as a clean
    ClickException, not a traceback) and leave nothing written for the
    album, rather than silently proceeding into asset generation."""
    import fcntl
    import os

    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir()
    album_dir = _build_album(pictures_root)

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"

    lock_path = album_dir / ".prep-publish.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _invoke_publish(
            monkeypatch, album_dir, pictures_root, site_root, users_path
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result.exit_code != 0
    assert "already being published" in result.output

    # Nothing written for this album: the lock must actually be held
    # around Phase 2 onward (asset generation/rendering/album.json), not
    # just acquired-and-released as a no-op somewhere unused.
    assert not (album_dir / ".web").exists()
    assert not (album_dir / "index.html").exists()
    assert not (album_dir / "picture").exists()


def test_publish_album_with_only_notes_falls_back_to_todays_date(tmp_path, monkeypatch):
    """An album whose items are all notes (no photos/videos at all) has no
    item date to derive earliest_date from -- _publish_album must fall back
    to date.today() rather than crashing on an empty min() call (Test
    Coverage review finding #7)."""
    import datetime

    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir()
    album_dir = pictures_root / "2024" / "Notes Only"
    album_dir.mkdir(parents=True)
    (album_dir / "publish.yaml").write_text(
        "title: \"Notes Only\"\n"
        "public: true\n"
        "items:\n"
        "  - note: \"Just some words, no pictures.\"\n"
    )

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"

    result = _invoke_publish(monkeypatch, album_dir, pictures_root, site_root, users_path)
    assert result.exit_code == 0, result.output

    summary = json.loads((album_dir / ".web" / "album.json").read_text())
    assert summary["earliest_date"] == datetime.date.today().isoformat()
    assert summary["covers"] == []


def test_publish_end_to_end_with_video_item(tmp_path, monkeypatch):
    """End-to-end `prep publish` against an album containing a real video
    item, exercising generate_video_poster's real-ffmpeg path end to end
    (Test Coverage review finding #5). Skips gracefully on a host with no
    ffmpeg/ffprobe installed (this sandbox); runs for real inside Task 16's
    container."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed in this test environment")

    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir()
    album_dir = pictures_root / "2024" / "Video Album"
    album_dir.mkdir(parents=True)

    shutil.copy(FIXTURES / "sample_with_exif.jpg", album_dir / "IMG_0001.jpg")
    shutil.copy(FIXTURES / "sample.mp4", album_dir / "VID_0001.mp4")

    (album_dir / "publish.yaml").write_text(
        "title: \"Video Album\"\n"
        "public: true\n"
        "items:\n"
        "  - file: IMG_0001.jpg\n"
        "  - file: VID_0001.mp4\n"
    )

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"

    result = _invoke_publish(monkeypatch, album_dir, pictures_root, site_root, users_path)
    assert result.exit_code == 0, result.output

    thumbs_dir = album_dir / ".web" / "thumbs"
    assert (thumbs_dir / "IMG_0001.jpg.jpg").exists()
    assert (thumbs_dir / "VID_0001.mp4.jpg").exists()

    index_html = (album_dir / "index.html").read_text()
    assert "VID_0001.mp4" in index_html

    picture_page = (album_dir / "picture" / "VID_0001.html").read_text()
    assert "VID_0001.mp4" in picture_page
    assert "video" in picture_page.lower()
