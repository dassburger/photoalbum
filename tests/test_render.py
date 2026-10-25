"""Tests for prep.render: shared static asset syncing and the project-wide
"no literal CSS property value in a style attribute" rule (see the plan's
Development Approach and Solution Overview's "Inline style attributes — the
one narrow exception" bullet).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from prep.render import STATIC_DIRNAME, sync_shared_static

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
