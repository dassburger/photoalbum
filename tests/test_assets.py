"""Tests for prep.assets: thumbnail/poster-frame generation and cleanup."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from prep.assets import (
    AssetError,
    cleanup_stale_thumbnails,
    generate_photo_thumbnail,
    generate_video_poster,
    pick_cover_thumbnails,
    thumbnail_filename,
    thumbnail_path,
    thumbs_dir,
)
from prep.manifest import ManifestItem

FIXTURES = Path(__file__).parent / "fixtures"


# --- generate_photo_thumbnail --------------------------------------------


def test_generate_photo_thumbnail_returns_dimensions_and_writes_file(tmp_path):
    dest = tmp_path / "thumb.jpg"
    width, height = generate_photo_thumbnail(
        FIXTURES / "sample_with_exif.jpg", dest, max_size=800
    )

    assert dest.exists()
    with Image.open(dest) as img:
        assert img.size == (width, height)
        assert img.format == "JPEG"


def test_generate_photo_thumbnail_preserves_aspect_ratio(tmp_path):
    # sample_rotated.jpg is 8x6 raw (before EXIF correction) -> 6x8 after
    # exif_transpose, aspect ratio 6/8 = 0.75.
    dest = tmp_path / "thumb.jpg"
    width, height = generate_photo_thumbnail(
        FIXTURES / "sample_rotated.jpg", dest, max_size=4
    )
    assert pytest.approx(width / height, rel=0.4) == 6 / 8


def test_generate_photo_thumbnail_respects_max_size(tmp_path):
    dest = tmp_path / "thumb.jpg"
    width, height = generate_photo_thumbnail(
        FIXTURES / "sample_with_exif.jpg", dest, max_size=4
    )
    assert width <= 4
    assert height <= 4


def test_generate_photo_thumbnail_applies_exif_orientation(tmp_path):
    # sample_rotated.jpg: raw 8x6 with a red marker at top-left and
    # Orientation=6 ("rotate 90 CW to display correctly"). Without
    # exif_transpose, the output would stay 8x6 with red still top-left;
    # with it (correctly applied), the output is right-side-up: 6x8, with
    # the red marker now in the top-right corner (verified empirically
    # against Pillow's own exif_transpose before this fixture was built).
    dest = tmp_path / "thumb.jpg"
    width, height = generate_photo_thumbnail(
        FIXTURES / "sample_rotated.jpg", dest, max_size=800
    )

    assert (width, height) == (6, 8)
    with Image.open(dest) as img:
        top_left = img.getpixel((0, 0))
        top_right = img.getpixel((width - 1, 0))

    # top-left should stay blue-ish (no marker there after correction);
    # top-right should now be the red marker (red channel clearly
    # dominant despite JPEG compression noise).
    assert top_left[2] > top_left[0]  # blue > red
    assert top_right[0] > top_right[2]  # red > blue


def test_generate_photo_thumbnail_raises_asset_error_on_corrupt_file(tmp_path):
    dest = tmp_path / "thumb.jpg"
    with pytest.raises(AssetError, match="sample_corrupt.jpg"):
        generate_photo_thumbnail(FIXTURES / "sample_corrupt.jpg", dest)
    assert not dest.exists()


def test_generate_photo_thumbnail_writes_atomically_no_partial_file_on_failure(
    tmp_path,
):
    dest = tmp_path / "thumb.jpg"
    with pytest.raises(AssetError):
        generate_photo_thumbnail(FIXTURES / "sample_corrupt.jpg", dest)
    # No leftover temp file in the destination directory either.
    assert list(tmp_path.iterdir()) == []


# --- generate_video_poster -----------------------------------------------


def test_generate_video_poster_raises_clearly_when_ffmpeg_missing(
    tmp_path, monkeypatch
):
    import prep.assets as assets_module

    monkeypatch.setattr(assets_module, "_ffmpeg_available", lambda: False)
    dest = tmp_path / "poster.jpg"
    with pytest.raises(AssetError, match="ffmpeg is not installed"):
        generate_video_poster(FIXTURES / "sample.mp4", dest)
    assert not dest.exists()


def test_generate_video_poster_raises_clearly_on_subprocess_failure(
    tmp_path, monkeypatch
):
    # Simulate ffmpeg being present but failing on this particular file
    # (e.g. a corrupt/unreadable video), entirely without needing a real
    # ffmpeg binary -- exercises the subprocess-failure error path.
    import subprocess

    import prep.assets as assets_module

    monkeypatch.setattr(assets_module, "_ffmpeg_available", lambda: True)

    class _FailedResult:
        returncode = 1
        stderr = "Invalid data found when processing input"

    monkeypatch.setattr(
        assets_module.subprocess, "run", lambda *a, **k: _FailedResult()
    )

    dest = tmp_path / "poster.jpg"
    with pytest.raises(AssetError, match="ffmpeg failed to extract"):
        generate_video_poster(FIXTURES / "sample.mp4", dest)
    assert not dest.exists()
    # No leftover temp file either.
    assert list(tmp_path.iterdir()) == []


def test_generate_video_poster_raises_clearly_when_output_unreadable(
    tmp_path, monkeypatch
):
    # ffmpeg reports success (returncode 0) but the file it wrote isn't a
    # readable image -- also must be caught, not silently accepted.
    import prep.assets as assets_module

    monkeypatch.setattr(assets_module, "_ffmpeg_available", lambda: True)

    class _OkResult:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **kwargs):
        # The destination path is the last argument in our ffmpeg command.
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"not a real jpeg")
        return _OkResult()

    monkeypatch.setattr(assets_module.subprocess, "run", _fake_run)

    dest = tmp_path / "poster.jpg"
    with pytest.raises(AssetError, match="unreadable poster frame"):
        generate_video_poster(FIXTURES / "sample.mp4", dest)
    assert not dest.exists()


def test_generate_video_poster_against_real_ffmpeg():
    # Exercises the real ffmpeg subprocess path end-to-end. Skips
    # gracefully (rather than failing) when ffmpeg isn't installed in
    # this test environment -- same pattern Task 3 used for ffprobe. This
    # sandbox has no ffmpeg installed, so this test is expected to skip
    # here; it should be re-verified for real once Task 16's container
    # (which installs ffmpeg) exists and runs the suite.
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed in this test environment")

    import tempfile

    dest = Path(tempfile.mkdtemp()) / "poster.jpg"
    width, height = generate_video_poster(FIXTURES / "sample.mp4", dest)
    assert dest.exists()
    with Image.open(dest) as img:
        assert img.format == "JPEG"
        assert img.size == (width, height)


# --- pick_cover_thumbnails -------------------------------------------------


def _items(*filenames_or_notes):
    items = []
    for value in filenames_or_notes:
        if value.startswith("NOTE:"):
            items.append(ManifestItem(note=value[len("NOTE:") :]))
        else:
            items.append(ManifestItem(file=value))
    return items


def test_pick_cover_thumbnails_small_list_returns_all():
    items = _items("a.jpg", "b.jpg")
    result = pick_cover_thumbnails(items, count=3)
    assert [i.file for i in result] == ["a.jpg", "b.jpg"]


def test_pick_cover_thumbnails_exact_count_returns_all_in_order():
    items = _items("a.jpg", "b.jpg", "c.jpg")
    result = pick_cover_thumbnails(items, count=3)
    assert [i.file for i in result] == ["a.jpg", "b.jpg", "c.jpg"]


def test_pick_cover_thumbnails_large_list_picks_first_middle_last():
    items = _items(*(f"img{i}.jpg" for i in range(10)))
    result = pick_cover_thumbnails(items, count=3)
    assert len(result) == 3
    assert result[0].file == "img0.jpg"
    assert result[-1].file == "img9.jpg"
    # the middle pick should be roughly in the middle of the range
    middle_index = int(result[1].file[3:-4])
    assert 3 <= middle_index <= 6


def test_pick_cover_thumbnails_skips_notes():
    items = _items("a.jpg", "NOTE:hello", "b.jpg")
    result = pick_cover_thumbnails(items, count=3)
    assert all(not i.is_note for i in result)
    assert [i.file for i in result] == ["a.jpg", "b.jpg"]


def test_pick_cover_thumbnails_empty_list_returns_empty():
    assert pick_cover_thumbnails([], count=3) == []


def test_pick_cover_thumbnails_all_notes_returns_empty():
    items = _items("NOTE:only a note")
    assert pick_cover_thumbnails(items, count=3) == []


def test_pick_cover_thumbnails_count_zero_returns_empty():
    items = _items("a.jpg", "b.jpg", "c.jpg")
    assert pick_cover_thumbnails(items, count=0) == []


# --- cleanup_stale_thumbnails ----------------------------------------------


def test_cleanup_stale_thumbnails_removes_only_unlisted_files(tmp_path):
    directory = thumbs_dir(tmp_path)
    directory.mkdir(parents=True)
    kept = directory / thumbnail_filename("IMG_0001.jpg")
    stale = directory / thumbnail_filename("IMG_0002.jpg")
    kept.write_bytes(b"kept")
    stale.write_bytes(b"stale")

    removed = cleanup_stale_thumbnails(tmp_path, current_filenames=["IMG_0001.jpg"])

    assert removed == [stale]
    assert kept.exists()
    assert not stale.exists()


def test_cleanup_stale_thumbnails_on_missing_directory_returns_empty(tmp_path):
    assert cleanup_stale_thumbnails(tmp_path, current_filenames=["a.jpg"]) == []


def test_cleanup_stale_thumbnails_no_stale_files_removes_nothing(tmp_path):
    directory = thumbs_dir(tmp_path)
    directory.mkdir(parents=True)
    kept = directory / thumbnail_filename("a.jpg")
    kept.write_bytes(b"data")

    removed = cleanup_stale_thumbnails(tmp_path, current_filenames=["a.jpg"])

    assert removed == []
    assert kept.exists()


def test_thumbnail_path_matches_thumbs_dir_and_filename_helpers(tmp_path):
    path = thumbnail_path(tmp_path, "IMG_0001.jpg")
    assert path == thumbs_dir(tmp_path) / thumbnail_filename("IMG_0001.jpg")
    assert path.name == "IMG_0001.jpg.jpg"


def test_thumbnail_filename_sanitizes_path_traversal(tmp_path):
    """Defensive hardening (Quality review finding #1): even though
    prep/manifest.py's validate_manifest now hard-rejects any `items` entry
    that escapes the album directory, thumbnail_filename/thumbnail_path
    must never translate a path-separator-containing filename into a
    generated path outside thumbs_dir() -- confirmed here by reproducing
    the exact escape from the review ("../private-album/secret.jpg")."""
    escaped = thumbnail_path(tmp_path / "album", "../private-album/secret.jpg")
    assert escaped.parent == thumbs_dir(tmp_path / "album")
    assert escaped.name == "secret.jpg.jpg"


def test_thumbnail_filename_sanitizes_plain_subdirectory(tmp_path):
    name = thumbnail_filename("subdir/photo.jpg")
    assert name == "photo.jpg.jpg"
    assert "/" not in name
