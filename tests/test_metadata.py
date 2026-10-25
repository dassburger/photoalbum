"""Tests for prep.metadata: EXIF/ffprobe date extraction with mtime fallback."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prep.metadata import (
    get_item_date,
    get_photo_date,
    get_video_date,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_get_photo_date_reads_exif_date_time_original():
    dt, is_fallback = get_photo_date(FIXTURES / "sample_with_exif.jpg")
    assert dt == datetime(2023, 6, 15, 14, 30, 0)
    assert is_fallback is False


def test_get_photo_date_falls_back_to_mtime_when_exif_missing(caplog):
    path = FIXTURES / "sample_no_exif.jpg"
    expected_mtime = datetime.fromtimestamp(path.stat().st_mtime)

    with caplog.at_level(logging.WARNING):
        dt, is_fallback = get_photo_date(path)

    assert is_fallback is True
    assert dt == expected_mtime
    assert any("fall" in record.message.lower() for record in caplog.records)


def test_get_photo_date_falls_back_on_corrupt_file(tmp_path, caplog):
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not a real jpeg at all")

    with caplog.at_level(logging.WARNING):
        dt, is_fallback = get_photo_date(corrupt)

    assert is_fallback is True
    assert dt == datetime.fromtimestamp(corrupt.stat().st_mtime)
    assert len(caplog.records) >= 1


def test_get_video_date_falls_back_to_mtime_when_ffprobe_unavailable(
    monkeypatch, caplog
):
    # Force the "ffprobe not installed" path regardless of the actual test
    # environment, so this fallback behavior is exercised unconditionally.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    import prep.metadata as metadata_module

    monkeypatch.setattr(metadata_module.shutil, "which", lambda name: None)

    path = FIXTURES / "sample.mp4"
    expected_mtime = datetime.fromtimestamp(path.stat().st_mtime)

    with caplog.at_level(logging.WARNING):
        dt, is_fallback = get_video_date(path)

    assert is_fallback is True
    assert dt == expected_mtime
    assert any("fall" in record.message.lower() for record in caplog.records)


def test_get_video_date_uses_ffprobe_when_available():
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is not installed in this test environment")
    # The placeholder sample.mp4 fixture has no real creation_time tag
    # (it's a minimal hand-built container, not a real ffmpeg-encoded
    # video), so even with ffprobe present this should still land on the
    # mtime fallback — this just confirms the ffprobe path doesn't crash
    # when the binary is actually present.
    path = FIXTURES / "sample.mp4"
    dt, is_fallback = get_video_date(path)
    assert isinstance(dt, datetime)
    assert isinstance(is_fallback, bool)


def test_get_video_date_converts_utc_creation_time_to_local(monkeypatch):
    import prep.metadata as metadata_module

    monkeypatch.setattr(metadata_module, "_ffprobe_available", lambda: True)
    monkeypatch.setattr(
        metadata_module,
        "_ffprobe_creation_time",
        lambda path: "2023-06-15T20:30:00.000000Z",
    )

    # Pin the local timezone to UTC-5 for a deterministic assertion,
    # independent of the machine running the tests, by patching
    # datetime.astimezone to always convert to that fixed offset (mirroring
    # what a real astimezone() call would do against that system timezone).
    fixed_offset = timezone(timedelta(hours=-5))

    dt_utc = datetime(2023, 6, 15, 20, 30, 0, tzinfo=timezone.utc)
    expected_local = dt_utc.astimezone(fixed_offset).replace(tzinfo=None)

    # datetime is a C immutable type, so its astimezone() can't be patched
    # in place. Instead, swap in a datetime subclass (strptime/replace
    # return instances of the calling class, so this flows through
    # get_video_date unchanged) whose astimezone() always converts to our
    # fixed offset, simulating what the real system timezone would do.
    class _FixedOffsetDateTime(datetime):
        def astimezone(self, tz=None):
            return super().astimezone(fixed_offset)

    monkeypatch.setattr(metadata_module, "datetime", _FixedOffsetDateTime)

    dt, is_fallback = get_video_date(FIXTURES / "sample.mp4")

    assert is_fallback is False
    assert dt == expected_local


def test_get_item_date_dispatches_to_photo_for_jpg():
    dt, is_fallback = get_item_date(FIXTURES / "sample_with_exif.jpg")
    assert dt == datetime(2023, 6, 15, 14, 30, 0)
    assert is_fallback is False


def test_get_item_date_dispatches_to_video_for_mp4(monkeypatch):
    import prep.metadata as metadata_module

    monkeypatch.setattr(metadata_module, "_ffprobe_available", lambda: False)
    path = FIXTURES / "sample.mp4"
    dt, is_fallback = get_item_date(path)
    assert is_fallback is True
    assert dt == datetime.fromtimestamp(path.stat().st_mtime)


def test_get_item_date_raises_for_unknown_extension(tmp_path):
    unknown = tmp_path / "file.txt"
    unknown.write_text("hello")
    with pytest.raises(ValueError):
        get_item_date(unknown)
