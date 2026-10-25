"""Direct unit tests for prep/atomic.py's atomic_write/atomic_write_text.

Regression test for iteration-2 review finding #5: this shared helper had
zero direct tests before this -- the chmod-to-target-mode behavior (the
actual fix for the original "generated files land as mode 0600" finding)
was only ever manually verified inside a throwaway container build, never
asserted by an automated test. A future edit that dropped the `os.chmod`
call in `atomic_write` would have gone undetected.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from prep.atomic import atomic_write, atomic_write_text


def test_atomic_write_defaults_to_mode_0644(tmp_path):
    dest = tmp_path / "out.txt"

    def _write(tmp_path: Path) -> None:
        tmp_path.write_text("hello\n")

    atomic_write(dest, _write)

    assert dest.read_text() == "hello\n"
    assert oct(dest.stat().st_mode & 0o777) == oct(0o644)


def test_atomic_write_honors_explicit_mode_override(tmp_path):
    dest = tmp_path / "secret.yml"

    def _write(tmp_path: Path) -> None:
        tmp_path.write_text("alice: hash\n")

    atomic_write(dest, _write, mode=0o600)

    assert dest.read_text() == "alice: hash\n"
    assert oct(dest.stat().st_mode & 0o777) == oct(0o600)


def test_atomic_write_text_defaults_to_mode_0644(tmp_path):
    dest = tmp_path / "index.html"
    atomic_write_text(dest, "<html></html>")
    assert dest.read_text() == "<html></html>"
    assert oct(dest.stat().st_mode & 0o777) == oct(0o644)


def test_atomic_write_text_honors_explicit_mode_override(tmp_path):
    dest = tmp_path / "users.yml"
    atomic_write_text(dest, "alice: hash\n", mode=0o600)
    assert dest.read_text() == "alice: hash\n"
    assert oct(dest.stat().st_mode & 0o777) == oct(0o600)


def test_atomic_write_creates_missing_parent_directories(tmp_path):
    dest = tmp_path / "nested" / "deeper" / "out.txt"
    atomic_write_text(dest, "content\n")
    assert dest.read_text() == "content\n"


def test_atomic_write_cleans_up_temp_file_and_leaves_dest_untouched_on_failure(
    tmp_path,
):
    """If write_fn raises, the temp file must be removed and dest (whether
    pre-existing or not) must be left exactly as it was -- the whole point
    of writing to a temp file first and only renaming at the very end."""
    dest = tmp_path / "out.txt"
    dest.write_text("original content\n")

    def _failing_write(tmp_path: Path) -> None:
        tmp_path.write_text("partial garbage")
        raise RuntimeError("simulated failure mid-write")

    with pytest.raises(RuntimeError):
        atomic_write(dest, _failing_write)

    # Original file completely untouched.
    assert dest.read_text() == "original content\n"
    # No leftover temp file in the destination's directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
    assert leftovers == []


def test_atomic_write_cleans_up_temp_file_when_dest_did_not_exist_yet(tmp_path):
    """Same exception-cleanup path, but for a brand-new dest that never
    existed before -- it must still not exist afterward, and no temp file
    should be left behind."""
    dest = tmp_path / "new_file.txt"

    def _failing_write(tmp_path: Path) -> None:
        raise RuntimeError("simulated failure before any content is written")

    with pytest.raises(RuntimeError):
        atomic_write(dest, _failing_write)

    assert not dest.exists()
    leftovers = list(tmp_path.iterdir())
    assert leftovers == []


def test_atomic_write_text_overwrites_existing_file_atomically(tmp_path):
    dest = tmp_path / "out.txt"
    atomic_write_text(dest, "first\n")
    atomic_write_text(dest, "second\n")
    assert dest.read_text() == "second\n"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
    assert leftovers == []


def test_atomic_write_logs_dest_path_at_debug(tmp_path, caplog):
    """A future edit that silently dropped this trace (the one place every
    `prep`-managed file write is guaranteed to pass through) should be
    caught by a test, not discovered by eyeballing log output."""
    dest = tmp_path / "out.txt"
    with caplog.at_level(logging.DEBUG, logger="prep.atomic"):
        atomic_write_text(dest, "hello\n")
    assert str(dest) in caplog.text
