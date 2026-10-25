"""End-to-end tests for `prep publish` orchestration (prep/cli.py).

Exercises the full pipeline against a small fixture album directory built
in a temp dir (a `work/` subdir, a stray unlisted JPEG, and a note in
`items`), and the hard-validation-abort path (unknown user). No video item
is included here since this sandbox has no ffmpeg/ffprobe installed —
following the same graceful-skip pattern already used by tests_metadata.py
and test_assets.py for the real-ffmpeg path.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from click.testing import CliRunner

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
    album_dir: Path, pictures_root: Path, site_root: Path, users_path: Path
):
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
            "--users-file",
            str(users_path),
        ],
    )


def test_publish_end_to_end(tmp_path):
    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir()
    album_dir = _build_album(pictures_root)

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"

    result = _invoke_publish(album_dir, pictures_root, site_root, users_path)
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


def test_publish_unknown_user_aborts_cleanly(tmp_path):
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

    result = _invoke_publish(album_dir, pictures_root, site_root, users_path)

    assert result.exit_code != 0
    assert "nobody" in result.output

    # nothing written for this album, and no site output either
    assert not (album_dir / ".web").exists()
    assert not (album_dir / "index.html").exists()
    assert not (album_dir / "picture").exists()
    assert not site_root.exists()


def test_publish_corrupt_image_aborts_without_partial_thumbnails(tmp_path):
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

    result = _invoke_publish(album_dir, pictures_root, site_root, users_path)

    assert result.exit_code != 0
    assert "IMG_0002.jpg" in result.output

    # The good item's thumbnail must not have been left behind even though
    # it was generated before the corrupt item failed — all-or-nothing.
    assert not (album_dir / ".web" / "thumbs").exists()
    assert not (album_dir / "index.html").exists()
