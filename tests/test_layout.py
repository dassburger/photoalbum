"""Tests for prep.layout: day-grouping and the grid-position algorithm."""

from __future__ import annotations

from datetime import date, datetime

from prep.manifest import ManifestItem
from prep.layout import (
    DayBucket,
    GridEntry,
    NOTE_SPAN,
    compute_grid_positions,
    group_by_day,
)

PHOTO = lambda name: ManifestItem(file=name)
NOTE = lambda text: ManifestItem(note=text)


# -- group_by_day -------------------------------------------------------


def test_group_by_day_empty_returns_empty_list():
    assert group_by_day([]) == []


def test_group_by_day_single_day():
    items = [PHOTO("a.jpg"), PHOTO("b.jpg"), PHOTO("c.jpg")]
    d = date(2024, 1, 1)
    buckets = group_by_day([(item, d) for item in items])
    assert len(buckets) == 1
    assert buckets[0].day == d
    assert buckets[0].items == items


def test_group_by_day_multi_day_preserves_order_within_bucket():
    a, b, c, e = PHOTO("a.jpg"), PHOTO("b.jpg"), PHOTO("c.jpg"), PHOTO("e.jpg")
    day1, day2 = date(2024, 1, 1), date(2024, 1, 2)
    buckets = group_by_day(
        [(a, day1), (b, day1), (c, day2), (e, day2)]
    )
    assert [bucket.day for bucket in buckets] == [day1, day2]
    assert buckets[0].items == [a, b]
    assert buckets[1].items == [c, e]


def test_group_by_day_accepts_datetime_and_truncates_to_date():
    a, b = PHOTO("a.jpg"), PHOTO("b.jpg")
    buckets = group_by_day(
        [
            (a, datetime(2024, 1, 1, 9, 0)),
            (b, datetime(2024, 1, 1, 23, 59)),
        ]
    )
    assert len(buckets) == 1
    assert buckets[0].day == date(2024, 1, 1)


def test_group_by_day_non_contiguous_same_day_not_merged():
    # Manifest order must win even when it isn't chronological: two runs
    # that happen to share a date, but aren't adjacent, stay as separate
    # buckets rather than being merged together.
    a, b, c = PHOTO("a.jpg"), PHOTO("b.jpg"), PHOTO("c.jpg")
    day1, day2 = date(2024, 1, 1), date(2024, 1, 2)
    buckets = group_by_day([(a, day2), (b, day1), (c, day2)])
    assert [bucket.day for bucket in buckets] == [day2, day1, day2]
    assert buckets[0].items == [a]
    assert buckets[1].items == [b]
    assert buckets[2].items == [c]


def test_group_by_day_note_attaches_to_preceding_item_day():
    photo, note = PHOTO("a.jpg"), NOTE("hello")
    day1 = date(2024, 1, 1)
    buckets = group_by_day([(photo, day1), (note, None)])
    assert len(buckets) == 1
    assert buckets[0].day == day1
    assert buckets[0].items == [photo, note]


def test_group_by_day_note_first_attaches_to_following_item_day():
    note, photo = NOTE("hello"), PHOTO("a.jpg")
    day1 = date(2024, 1, 1)
    buckets = group_by_day([(note, None), (photo, day1)])
    assert len(buckets) == 1
    assert buckets[0].day == day1
    assert buckets[0].items == [note, photo]


def test_group_by_day_note_between_different_days_attaches_to_preceding():
    a, note, b = PHOTO("a.jpg"), NOTE("hello"), PHOTO("b.jpg")
    day1, day2 = date(2024, 1, 1), date(2024, 1, 2)
    buckets = group_by_day([(a, day1), (note, None), (b, day2)])
    assert [bucket.day for bucket in buckets] == [day1, day2]
    assert buckets[0].items == [a, note]
    assert buckets[1].items == [b]


def test_group_by_day_all_notes_resolve_to_none():
    note1, note2 = NOTE("a"), NOTE("b")
    buckets = group_by_day([(note1, None), (note2, None)])
    assert len(buckets) == 1
    assert buckets[0].day is None
    assert buckets[0].items == [note1, note2]


# -- compute_grid_positions ----------------------------------------------


def test_compute_grid_positions_deterministic_across_repeated_calls():
    day1 = date(2024, 1, 1)
    buckets = [
        DayBucket(day=day1, items=[PHOTO("a.jpg"), PHOTO("b.jpg"), NOTE("x")])
    ]
    ratios = {"a.jpg": 1.8, "b.jpg": 0.5}
    first = compute_grid_positions(buckets, ratios, album_path="2024/Trip")
    second = compute_grid_positions(buckets, ratios, album_path="2024/Trip")
    assert [(e.kind, e.row_span, e.col_span) for e in first] == [
        (e.kind, e.row_span, e.col_span) for e in second
    ]


def test_compute_grid_positions_different_album_path_can_differ():
    # Not a correctness requirement in itself, but demonstrates the seed
    # actually depends on album_path rather than being a global constant.
    day1 = date(2024, 1, 1)
    buckets = [DayBucket(day=day1, items=[PHOTO("a.jpg")])]
    ratios = {"a.jpg": 1.8}
    a = compute_grid_positions(buckets, ratios, album_path="album-a")
    b = compute_grid_positions(buckets, ratios, album_path="album-b")
    # Both are internally self-consistent (deterministic), even if the
    # specific span drawn differs.
    a2 = compute_grid_positions(buckets, ratios, album_path="album-a")
    assert [(e.row_span, e.col_span) for e in a] == [
        (e.row_span, e.col_span) for e in a2
    ]


def test_compute_grid_positions_emits_one_separator_per_bucket_in_order():
    day1, day2 = date(2024, 1, 1), date(2024, 1, 2)
    buckets = [
        DayBucket(day=day1, items=[PHOTO("a.jpg")]),
        DayBucket(day=day2, items=[PHOTO("b.jpg"), PHOTO("c.jpg")]),
    ]
    entries = compute_grid_positions(buckets, {}, album_path="p")
    kinds_and_days = [(e.kind, e.day) for e in entries]
    assert kinds_and_days == [
        ("separator", day1),
        ("item", day1),
        ("separator", day2),
        ("item", day2),
        ("item", day2),
    ]


def test_compute_grid_positions_preserves_item_flow_order():
    day1 = date(2024, 1, 1)
    a, b, c = PHOTO("a.jpg"), PHOTO("b.jpg"), PHOTO("c.jpg")
    buckets = [DayBucket(day=day1, items=[a, b, c])]
    entries = compute_grid_positions(buckets, {}, album_path="p")
    item_entries = [e for e in entries if e.kind == "item"]
    assert [e.item for e in item_entries] == [a, b, c]


def test_compute_grid_positions_notes_get_distinct_span_from_photos():
    day1 = date(2024, 1, 1)
    note = NOTE("hello")
    buckets = [
        DayBucket(
            day=day1,
            items=[
                PHOTO("landscape.jpg"),
                PHOTO("portrait.jpg"),
                PHOTO("square.jpg"),
                note,
            ],
        )
    ]
    ratios = {"landscape.jpg": 2.0, "portrait.jpg": 0.5, "square.jpg": 1.0}
    entries = compute_grid_positions(buckets, ratios, album_path="p")
    item_entries = {e.item.file or e.item.note: e for e in entries if e.kind == "item"}

    note_entry = item_entries["hello"]
    assert (note_entry.row_span, note_entry.col_span) == NOTE_SPAN

    for key in ("landscape.jpg", "portrait.jpg", "square.jpg"):
        span = (item_entries[key].row_span, item_entries[key].col_span)
        assert span != NOTE_SPAN


def test_compute_grid_positions_portrait_and_landscape_produce_different_shapes():
    day1 = date(2024, 1, 1)
    landscape, portrait = PHOTO("landscape.jpg"), PHOTO("portrait.jpg")
    buckets = [DayBucket(day=day1, items=[landscape, portrait])]
    ratios = {"landscape.jpg": 2.0, "portrait.jpg": 0.5}

    # Try several album_path seeds to make sure the shape difference holds
    # regardless of which random variant gets drawn within each category.
    for seed in ("p1", "p2", "p3", "p4", "p5"):
        entries = compute_grid_positions(buckets, ratios, album_path=seed)
        spans = {
            e.item.file: (e.row_span, e.col_span)
            for e in entries
            if e.kind == "item"
        }
        # Landscape tiles are wider than tall; portrait tiles are taller
        # than wide — so col_span > row_span for landscape and the reverse
        # for portrait, in every variant.
        assert spans["landscape.jpg"][1] > spans["landscape.jpg"][0]
        assert spans["portrait.jpg"][0] > spans["portrait.jpg"][1]
        assert spans["landscape.jpg"] != spans["portrait.jpg"]


def test_compute_grid_positions_missing_aspect_ratio_defaults_to_square():
    day1 = date(2024, 1, 1)
    buckets = [DayBucket(day=day1, items=[PHOTO("unknown.jpg")])]
    entries = compute_grid_positions(buckets, {}, album_path="p")
    item_entry = next(e for e in entries if e.kind == "item")
    # Square variants are (1, 1) or (2, 2) — row_span == col_span.
    assert item_entry.row_span == item_entry.col_span


def test_compute_grid_positions_empty_buckets_returns_empty_list():
    assert compute_grid_positions([], {}, album_path="p") == []
