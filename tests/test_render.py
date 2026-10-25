"""Tests for prep.render: shared static asset syncing, album-page rendering,
and the project-wide "no literal CSS property value in a style attribute"
rule (see the plan's Development Approach and Solution Overview's "Inline
style attributes — the one narrow exception" bullet).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from prep.manifest import AlbumInfo, Manifest, ManifestItem
from prep.render import STATIC_DIRNAME, render_album_page, sync_shared_static

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "prep" / "templates"
STATIC_SRC_DIR = Path(__file__).resolve().parent.parent / "prep" / "static"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_STYLE_ATTR_RE = re.compile(r'style\s*=\s*(["\'])(.*?)\1', re.DOTALL)


def _iter_style_attributes(html: str):
    """Yield each style="..."/style='...' attribute's raw value found in html."""
    for match in _STYLE_ATTR_RE.finditer(html):
        yield match.group(2)


def _assert_only_custom_properties(style_value: str, source: str):
    """Fail with a clear message if style_value sets any literal CSS property.

    Only `--custom-property: value;` declarations are allowed (per the
    plan's narrowed rule) — anything else (e.g. `color: red`, `display:
    none`) is a hard failure, identifying the offending file and
    declaration.
    """
    for declaration in style_value.split(";"):
        declaration = declaration.strip()
        if not declaration:
            continue
        prop = declaration.split(":", 1)[0].strip()
        assert prop.startswith("--"), (
            f"{source}: style attribute sets a literal CSS property "
            f"({declaration!r}) — only --custom-property declarations are "
            f"allowed in a style attribute; move {prop!r} into a CSS file "
            f"instead (full attribute: {style_value!r})"
        )


def _html_files():
    files = sorted(TEMPLATES_DIR.glob("*.html"))
    if FIXTURES_DIR.exists():
        files += sorted(FIXTURES_DIR.rglob("*.html"))
    return files


# -- no-literal-CSS-in-style-attributes enforcement ----------------------


def test_no_html_files_found_is_not_silently_vacuous():
    # Guards against this test suite passing trivially because the glob
    # patterns above stopped matching anything.
    assert list(TEMPLATES_DIR.glob("*.html")), "expected at least base.html to exist"


@pytest.mark.parametrize("html_path", _html_files(), ids=lambda p: p.name)
def test_style_attributes_set_only_custom_properties(html_path: Path):
    # Not an error for a template to have no style attribute at all (most
    # won't) — this test only enforces the *shape* of ones that do exist.
    html = html_path.read_text()
    for style_value in _iter_style_attributes(html):
        _assert_only_custom_properties(style_value, source=str(html_path))


def test_custom_property_only_style_attribute_is_accepted():
    # Sanity check for the checker itself: a well-formed custom-property
    # style attribute (the one narrow exception) must not be flagged.
    _assert_only_custom_properties(
        "--row-span: 2; --col-span: 3", source="<inline test>"
    )


def test_literal_css_property_in_style_attribute_is_rejected():
    with pytest.raises(AssertionError):
        _assert_only_custom_properties("color: red", source="<inline test>")


def test_mixed_custom_property_and_literal_is_rejected():
    with pytest.raises(AssertionError):
        _assert_only_custom_properties(
            "--row-span: 2; display: none", source="<inline test>"
        )


# -- sync_shared_static ---------------------------------------------------


def test_sync_shared_static_copies_all_static_files(tmp_path: Path):
    site_root = tmp_path / "site"
    dest = sync_shared_static(site_root)

    assert dest == site_root / STATIC_DIRNAME
    expected_names = {p.name for p in STATIC_SRC_DIR.iterdir() if p.is_file()}
    copied_names = {p.name for p in dest.iterdir() if p.is_file()}
    assert expected_names <= copied_names
    assert (dest / "base.css").read_text() == (STATIC_SRC_DIR / "base.css").read_text()


def test_sync_shared_static_is_idempotent_and_removes_stale_files(tmp_path: Path):
    site_root = tmp_path / "site"
    dest = sync_shared_static(site_root)

    # Simulate a stale file left behind by a previous version of
    # prep/static/ that no longer exists in the package.
    stale = dest / "old-leftover.css"
    stale.write_text("/* stale */")
    assert stale.exists()

    sync_shared_static(site_root)

    assert not stale.exists()
    assert (dest / "base.css").exists()


def test_sync_shared_static_creates_site_root_if_missing(tmp_path: Path):
    site_root = tmp_path / "does" / "not" / "exist" / "yet"
    dest = sync_shared_static(site_root)
    assert dest.is_dir()
    assert (dest / "base.css").exists()


# -- render_album_page -----------------------------------------------------


def _build_album(tmp_path: Path) -> AlbumInfo:
    """A small fixture album: two days, a note, no thumbnails pre-generated."""
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    for name in ("IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"):
        shutil.copy(FIXTURES_DIR / "sample_no_exif.jpg", album_dir / name)

    import os
    from datetime import datetime

    day1 = datetime(2024, 1, 1, 10, 0).timestamp()
    day2 = datetime(2024, 1, 2, 10, 0).timestamp()
    os.utime(album_dir / "IMG_0001.jpg", (day1, day1))
    os.utime(album_dir / "IMG_0002.jpg", (day1, day1))
    os.utime(album_dir / "IMG_0003.jpg", (day2, day2))

    manifest = Manifest(
        album_dir=album_dir,
        title="Test Album",
        public=True,
        allowed_users=[],
        items=[
            ManifestItem(file="IMG_0001.jpg"),
            ManifestItem(note="A great day."),
            ManifestItem(file="IMG_0002.jpg"),
            ManifestItem(file="IMG_0003.jpg"),
        ],
    )
    return AlbumInfo(path=album_dir, manifest=manifest)


def test_render_album_page_writes_index_html(tmp_path: Path):
    album_info = _build_album(tmp_path)
    dest = render_album_page(album_info, album_info.path)
    assert dest == album_info.path / "index.html"
    assert dest.exists()


def test_render_album_page_one_entry_per_item_in_authoritative_order(tmp_path: Path):
    album_info = _build_album(tmp_path)
    html = render_album_page(album_info, album_info.path).read_text()

    # Exactly one tile per photo item, one note-slot for the note.
    assert html.count('class="album-tile"') == 3
    assert html.count('class="note-slot"') == 1

    # Manifest order is authoritative: IMG_0001, note, IMG_0002, IMG_0003.
    pos_img1 = html.index('alt="IMG_0001.jpg"')
    pos_note = html.index("A great day.")
    pos_img2 = html.index('alt="IMG_0002.jpg"')
    pos_img3 = html.index('alt="IMG_0003.jpg"')
    assert pos_img1 < pos_note < pos_img2 < pos_img3


def test_render_album_page_separator_per_day_boundary(tmp_path: Path):
    album_info = _build_album(tmp_path)
    html = render_album_page(album_info, album_info.path).read_text()

    # Two distinct days -> exactly two separators, each labeled correctly.
    assert html.count('class="day-separator"') == 2
    assert "January 01, 2024" in html
    assert "January 02, 2024" in html


def test_render_album_page_links_and_thumbnail_paths(tmp_path: Path):
    album_info = _build_album(tmp_path)
    html = render_album_page(album_info, album_info.path).read_text()

    assert 'href="picture/IMG_0001.html"' in html
    assert 'src=".web/thumbs/IMG_0001.jpg.jpg"' in html


def test_render_album_page_only_custom_property_style_attributes(tmp_path: Path):
    album_info = _build_album(tmp_path)
    html = render_album_page(album_info, album_info.path).read_text()
    for style_value in _iter_style_attributes(html):
        _assert_only_custom_properties(style_value, source="rendered album page")


def test_render_album_page_single_day_no_separator_duplication(tmp_path: Path):
    album_dir = tmp_path / "single_day_album"
    album_dir.mkdir()
    shutil.copy(FIXTURES_DIR / "sample_no_exif.jpg", album_dir / "only.jpg")

    manifest = Manifest(
        album_dir=album_dir,
        title="Single Day",
        public=True,
        allowed_users=[],
        items=[ManifestItem(file="only.jpg")],
    )
    album_info = AlbumInfo(path=album_dir, manifest=manifest)

    html = render_album_page(album_info, album_dir).read_text()
    assert html.count('class="day-separator"') == 1
    assert html.count('class="album-tile"') == 1
