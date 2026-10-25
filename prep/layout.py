"""Day-grouping and irregular grid position algorithm for album pages.

Consumes a manifest's `items` in their already-authoritative order (see
prep/manifest.py's ordering rule) together with a per-item date, and never
re-sorts that order itself — `group_by_day` only decides where day-separator
boundaries fall, and `compute_grid_positions` only assigns each item a
deterministic grid-row/grid-column *span* (not an explicit position),
relying on the grid's own `grid-auto-flow: row` plus document order to make
"which came first" obvious, per the plan's Solution Overview.

`compute_grid_positions` accepts aspect ratios as a plain mapping (filename
-> width/height) rather than depending on prep/assets.py, since Task 6
(thumbnail generation, which produces real aspect ratios) doesn't exist yet.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Sequence

from prep.manifest import ManifestItem

# Candidate (row_span, col_span) pairs for photo/video tiles, keyed by
# orientation category. Deliberately disjoint across categories (and from
# NOTE_SPAN below) so portrait/landscape/note tiles are always visually
# distinguishable, never just a coincidence of the random draw.
_LANDSCAPE_VARIANTS: tuple[tuple[int, int], ...] = ((1, 2), (2, 3))
_PORTRAIT_VARIANTS: tuple[tuple[int, int], ...] = ((2, 1), (3, 2))
_SQUARE_VARIANTS: tuple[tuple[int, int], ...] = ((1, 1), (2, 2))

# Notes always get this same fixed, wide span rather than a randomized one
# (per the plan: "notes get a wider span") — and it's disjoint from every
# photo/video variant above, so a note's tile shape is never confusable with
# a photo's.
NOTE_SPAN: tuple[int, int] = (1, 3)

# Aspect-ratio (width / height) thresholds separating "landscape", "square",
# and "portrait" categories. A photo within [_PORTRAIT_MAX, _LANDSCAPE_MIN]
# is treated as roughly square.
_LANDSCAPE_MIN = 1.15
_PORTRAIT_MAX = 0.87

# Used when an item has no aspect ratio available (e.g. ratio data not
# supplied yet) — treated as square rather than erroring, since this module
# must not depend on Task 6 having run.
_DEFAULT_ASPECT_RATIO = 1.0


@dataclass
class DayBucket:
    """One contiguous run of manifest items sharing the same local day.

    `day` is a `datetime.date` (or `None` if every item in this run is a
    note with no resolvable neighbor date at all — only possible for an
    all-notes manifest). `items` preserves the items' original manifest
    order exactly.
    """

    day: date | None
    items: list[ManifestItem] = field(default_factory=list)


@dataclass
class GridEntry:
    """One entry in the album page's flow order: a day separator or a tile.

    `row_span`/`col_span` are the CSS grid spans to apply (fed into the
    template as `--row-span`/`--col-span` custom properties, per the
    Solution Overview's custom-property-only `style` rule — this module
    never produces literal CSS). Separators carry no item and a span of
    (1, 1) since they aren't placed via this mechanism.
    """

    kind: str  # "separator" | "item"
    day: date | None = None
    item: ManifestItem | None = None
    row_span: int = 1
    col_span: int = 1


def _as_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


def group_by_day(
    items_with_dates: Sequence[tuple[ManifestItem, date | datetime | None]],
) -> list[DayBucket]:
    """Group manifest items into ordered, contiguous same-day buckets.

    `items_with_dates` must be in the manifest's own authoritative order
    (see prep/manifest.py) — this function never reorders it, including
    when two non-adjacent runs happen to share the same date; it only
    decides where a new bucket starts.

    Each pair is `(item, date_or_none)`; `date_or_none` is `None` for a
    note (notes have no date of their own). A note's day is resolved to
    the day of the item immediately preceding it, or — if the note is
    first, or every item before it is also a note — the day of the next
    dated item after it. An all-notes input resolves every day to `None`.
    """
    if not items_with_dates:
        return []

    resolved: list[date | None] = [_as_date(d) for _, d in items_with_dates]

    for i in range(1, len(resolved)):
        if resolved[i] is None:
            resolved[i] = resolved[i - 1]

    for i in range(len(resolved) - 2, -1, -1):
        if resolved[i] is None:
            resolved[i] = resolved[i + 1]

    buckets: list[DayBucket] = []
    for (item, _), day in zip(items_with_dates, resolved):
        if buckets and buckets[-1].day == day:
            buckets[-1].items.append(item)
        else:
            buckets.append(DayBucket(day=day, items=[item]))
    return buckets


def _photo_span(aspect_ratio: float | None, rng: random.Random) -> tuple[int, int]:
    ratio = _DEFAULT_ASPECT_RATIO if aspect_ratio is None else aspect_ratio
    if ratio >= _LANDSCAPE_MIN:
        variants = _LANDSCAPE_VARIANTS
    elif ratio <= _PORTRAIT_MAX:
        variants = _PORTRAIT_VARIANTS
    else:
        variants = _SQUARE_VARIANTS
    return rng.choice(variants)


def compute_grid_positions(
    day_buckets: Sequence[DayBucket],
    aspect_ratios: Mapping[str, float],
    album_path: str = "",
) -> list[GridEntry]:
    """Assign each item a deterministic grid-row/grid-column span.

    Emits one `GridEntry` per day separator (one per bucket, in order) plus
    one per item, all in the buckets' own flow order — that document order,
    combined with `grid-auto-flow: row` in CSS, is the "obviously which
    came first" cue; no separate sequence number is computed here.

    `aspect_ratios` maps an item's `file` to its width/height ratio (as
    plain data — this module has no dependency on prep/assets.py, which
    doesn't exist yet). An item missing from the mapping (or a note, which
    has no `file`) is treated as square.

    The "randomized" tile-size variety is seeded deterministically from
    `album_path` plus each item's position in the overall flow, so
    re-running against an unchanged album produces identical output.
    """
    entries: list[GridEntry] = []
    index = 0
    for bucket in day_buckets:
        entries.append(GridEntry(kind="separator", day=bucket.day))
        for item in bucket.items:
            rng = random.Random(f"{album_path}:{index}")
            if item.is_note:
                row_span, col_span = NOTE_SPAN
            else:
                ratio = aspect_ratios.get(item.file)
                row_span, col_span = _photo_span(ratio, rng)
            entries.append(
                GridEntry(
                    kind="item",
                    day=bucket.day,
                    item=item,
                    row_span=row_span,
                    col_span=col_span,
                )
            )
            index += 1
    return entries
