"""Display kinds and eligibility (SPEC-09 C4): the single source of truth for which display a result can take.

The JavaScript copy (``neuronova-frontend/src/components/answers/eligibility.js``) must give the same answers; both are
tested against ``tests/fixtures/display_eligibility.json``. The numeric plotting policy is Stage 6B's: at most 15
significant digits, magnitude between 1e-12 and 1e15 (or zero), and no two different exact values that collapse to
one plotted number. It applies to the displays that *plot* numbers; a KPI, a ranked list, a pivot and the table show
exact strings and are not subject to it.
"""
from __future__ import annotations

import calendar
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .types import OUTPUT_TYPES

KIND_BY_PG_TYPE: dict[str, str] = {
    "int2": "integer", "int4": "integer", "int8": "integer",
    "numeric": "decimal",
    "float4": "number", "float8": "number",
    "text": "text", "varchar": "text", "bpchar": "text",
    "bool": "boolean",
    "uuid": "uuid",
    "date": "date", "timestamp": "timestamp", "timestamptz": "timestamptz",
}
NUMERIC_KINDS = frozenset({"integer", "decimal", "number"})
CATEGORY_KINDS = frozenset({"text", "boolean", "uuid"})
TEMPORAL_KINDS = frozenset({"date", "timestamp", "timestamptz"})

MAX_SIGNIFICANT_DIGITS = 15
MAX_PLOT_MAGNITUDE = 1e15
MIN_NON_ZERO_PLOT_MAGNITUDE = 1e-12
MAX_RANKED_ROWS = 100
MAX_BAR_ROWS = 100
MAX_GROUPED_X = 100
MAX_GROUPED_SERIES = 12
MAX_MULTI_LINE_SERIES = 8
MAX_PIE_ROWS = 6
MAX_SCATTER_ROWS = 500
MAX_HISTOGRAM_ROWS = 100
MAX_HEATMAP_SIDE = 50
MAX_PIVOT_SERIES = 30

_DECIMAL = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_DECIMAL_PARTS = re.compile(r"^(-?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TIMESTAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$")
_ZONED = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$", re.IGNORECASE)


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: str | None = None


OK = Eligibility(True)


def _no(reason: str) -> Eligibility:
    return Eligibility(False, reason)


def kind_of(pg_type: str) -> str | None:
    return KIND_BY_PG_TYPE.get(pg_type)


def cell_text(cell: Any) -> str | None:
    value = getattr(cell, "value", None)
    return value if isinstance(value, str) else None


def canonical_decimal(value: str) -> str | None:
    """Same digits-and-exponent identity as the browser's ``canonicalDecimal``."""
    text = value.strip()
    if not _DECIMAL.match(text):
        return None
    match = _DECIMAL_PARTS.match(text)
    if not match:
        return None
    sign, integer, fraction, exponent_text = match.group(1), match.group(2) or "", match.group(3) or "", match.group(4) or "0"
    digits = (integer + fraction).lstrip("0")
    if not digits:
        return "0"
    exponent = int(exponent_text) - len(fraction)
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    return f"{'-' if sign == '-' else ''}{stripped}e{exponent}"


def significant_digits(value: str) -> int:
    canonical = canonical_decimal(value)
    if canonical is None or canonical == "0":
        return 1
    return len(canonical.lstrip("-").split("e", 1)[0])


def _plot_number(cell: Any) -> tuple[Eligibility, str | None, float | None]:
    """``(verdict, identity, approximation)`` for one numeric cell under the Stage 6B policy."""
    kind = getattr(cell, "kind", None)
    if kind == "null" or getattr(cell, "value", None) is None:
        return _no("missing_value"), None, None
    if kind not in NUMERIC_KINDS:
        return _no("value_not_numeric"), None, None
    exact = cell_text(cell)
    if exact is None or not _DECIMAL.match(exact.strip()):
        return _no("value_not_numeric"), None, None
    if significant_digits(exact) > MAX_SIGNIFICANT_DIGITS:
        return _no("numeric_precision_exceeds_plot_policy"), None, None
    try:
        approximation = float(exact)
    except (OverflowError, ValueError):
        return _no("value_not_numeric"), None, None
    if not math.isfinite(approximation):
        return _no("value_not_numeric"), None, None
    magnitude = abs(approximation)
    if magnitude > MAX_PLOT_MAGNITUDE or (magnitude != 0 and magnitude < MIN_NON_ZERO_PLOT_MAGNITUDE):
        return _no("numeric_magnitude_outside_plot_policy"), None, None
    identity = canonical_decimal(exact)
    if identity is None:
        return _no("value_not_numeric"), None, None
    return OK, identity, approximation


def _plottable(cells: Sequence[Any]) -> Eligibility:
    """The numeric plotting policy over a column of cells, including the approximation-collision rule."""
    seen: dict[float, str] = {}
    for cell in cells:
        verdict, identity, approximation = _plot_number(cell)
        if not verdict.ok:
            return verdict
        known = seen.get(approximation)
        if known is not None and known != identity:
            return _no("numeric_precision_collision")
        seen[approximation] = identity  # type: ignore[assignment]
    return OK


def _valid_ymd(year: int, month: int, day: int) -> bool:
    # Years below 1000 are refused so the browser's Date.UTC (which maps 0-99 to 1900-1999) agrees with this check.
    return 1000 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]


def _valid_time(cell: Any) -> bool:
    kind, text = getattr(cell, "kind", None), cell_text(cell)
    if text is None:
        return False
    if kind == "date":
        match = _DATE.match(text)
        return bool(match) and _valid_ymd(*(int(g) for g in match.groups()))
    if kind == "timestamptz":
        if not _ZONED.search(text):
            return False
        text = _ZONED.sub("", text)
    elif kind != "timestamp":
        return False
    match = _TIMESTAMP.match(text)
    if not match:
        return False
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    return _valid_ymd(year, month, day) and hour < 24 and minute < 60 and second < 60


def _category_text(cell: Any) -> str | None:
    kind = getattr(cell, "kind", None)
    if kind not in CATEGORY_KINDS or getattr(cell, "value", None) is None:
        return None
    if kind == "boolean":
        return "true" if cell.value else "false"
    return cell_text(cell)


def _column_index(columns: Sequence[tuple[str, str]]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, (label, _) in enumerate(columns):
        index.setdefault(label, position)
    return index


def check_display(
    display: str,
    encodings: dict[str, Any],
    columns: Sequence[tuple[str, str]],
    rows: Sequence[Sequence[Any]],
) -> Eligibility:
    """Can this result take ``display`` with these encodings? ``columns`` are ``(label, pg_type)``."""
    if display not in OUTPUT_TYPES:
        return _no("unknown_display")
    if display in ("table", "narrative", "takeaways"):
        return OK
    labels = _column_index(columns)
    kinds = [kind_of(t) for _, t in columns]

    def col(label: str | None) -> int | None:
        return labels.get(label) if isinstance(label, str) else None

    def kind_at(position: int | None) -> str | None:
        return kinds[position] if position is not None else None

    def values(position: int) -> list[Any]:
        return [row[position] for row in rows]

    x_label, series_label = encodings.get("x"), encodings.get("series")
    label_label, value_label = encodings.get("label"), encodings.get("value")
    y_labels = [y for y in (encodings.get("y") or []) if isinstance(y, str)]
    row_count = len(rows)

    if display == "kpi":
        if row_count != 1:
            return _no("needs_one_row")
        position = col(value_label) if value_label else (0 if len(columns) == 1 else None)
        if position is None:
            return _no("value_column_missing")
        if kind_at(position) not in NUMERIC_KINDS:
            return _no("value_not_numeric")
        return _no("missing_value") if getattr(rows[0][position], "value", None) is None else OK

    if display == "kpi_comparison":
        if row_count != 1:
            return _no("needs_one_row")
        if len(y_labels) != 2:
            return _no("needs_two_numeric_columns")
        positions = [col(y) for y in y_labels]
        if any(p is None for p in positions):
            return _no("unknown_label")
        if any(kind_at(p) not in NUMERIC_KINDS for p in positions):
            return _no("value_not_numeric")
        if any(getattr(rows[0][p], "value", None) is None for p in positions):
            return _no("missing_value")
        return OK

    if row_count == 0:
        return _no("empty_result")

    if display == "ranked_list":
        lp, vp = col(label_label), col(value_label)
        if lp is None or vp is None:
            return _no("unknown_label")
        if kind_at(lp) not in CATEGORY_KINDS:
            return _no("label_not_category")
        if kind_at(vp) not in NUMERIC_KINDS:
            return _no("value_not_numeric")
        if row_count > MAX_RANKED_ROWS:
            return _no("too_many_rows")
        if any(_category_text(r[lp]) is None or getattr(r[vp], "value", None) is None for r in rows):
            return _no("missing_value")
        return OK

    if display in ("bar", "line", "area"):
        if len(columns) != 2:
            return _no("result_shape")
        want_x = kind_at(0)
        if display == "bar" and want_x not in CATEGORY_KINDS:
            return _no("x_not_category")
        if display != "bar" and want_x not in TEMPORAL_KINDS:
            return _no("x_not_temporal")
        if kind_at(1) not in NUMERIC_KINDS:
            return _no("value_not_numeric")
        if (x_label is not None and x_label != columns[0][0]) or (y_labels and y_labels[0] != columns[1][0]):
            return _no("encodings_mismatch")
        if row_count > MAX_BAR_ROWS:
            return _no("too_many_rows")
        verdict = _plottable(values(1))
        if not verdict.ok:
            return verdict
        seen: set[str] = set()
        for row in rows:
            if display == "bar":
                text = _category_text(row[0])
                if text is None:
                    return _no("missing_value")
                if text in seen:
                    return _no("duplicate_category")
            else:
                if not _valid_time(row[0]):
                    return _no("invalid_time")
                text = cell_text(row[0]) or ""
                if text in seen:
                    return _no("duplicate_time")
            seen.add(text)
        return OK

    if display == "pie":
        lp, vp = col(label_label), col(value_label)
        if lp is None or vp is None:
            return _no("unknown_label")
        if kind_at(lp) not in CATEGORY_KINDS:
            return _no("label_not_category")
        if kind_at(vp) not in NUMERIC_KINDS:
            return _no("value_not_numeric")
        if row_count > MAX_PIE_ROWS:
            return _no("too_many_rows")
        if any(_category_text(r[lp]) is None for r in rows):
            return _no("missing_value")
        verdict = _plottable(values(vp))
        if not verdict.ok:
            return verdict
        if any((_plot_number(r[vp])[2] or 0.0) < 0 for r in rows):
            return _no("negative_value")
        return OK

    if display == "scatter":
        xp, yp = col(x_label), col(y_labels[0] if y_labels else None)
        if xp is None or yp is None:
            return _no("unknown_label")
        if kind_at(xp) not in NUMERIC_KINDS or kind_at(yp) not in NUMERIC_KINDS:
            return _no("value_not_numeric")
        if row_count > MAX_SCATTER_ROWS:
            return _no("too_many_rows")
        for position in (xp, yp):
            verdict = _plottable(values(position))
            if not verdict.ok:
                return verdict
        return OK

    if display == "histogram":
        xp, yp = col(x_label), col(y_labels[0] if y_labels else None)
        if xp is None or yp is None:
            return _no("unknown_label")
        if kind_at(xp) not in CATEGORY_KINDS | NUMERIC_KINDS:
            return _no("x_not_category")
        if kind_at(yp) not in NUMERIC_KINDS:
            return _no("value_not_numeric")
        if row_count > MAX_HISTOGRAM_ROWS:
            return _no("too_many_rows")
        if any(getattr(r[xp], "value", None) is None for r in rows):
            return _no("missing_value")
        return _plottable(values(yp))

    # grouped_bar, multi_line, heatmap, pivot all need x, series and one numeric value.
    value_position = col(value_label) if display in ("heatmap", "pivot") else col(y_labels[0] if y_labels else None)
    xp, sp = col(x_label), col(series_label)
    if xp is None or sp is None or value_position is None:
        return _no("unknown_label")
    if kind_at(sp) not in CATEGORY_KINDS:
        return _no("series_not_category")
    if kind_at(value_position) not in NUMERIC_KINDS:
        return _no("value_not_numeric")
    x_kind = kind_at(xp)
    if display in ("grouped_bar", "heatmap") and x_kind not in CATEGORY_KINDS:
        return _no("x_not_category")
    if display == "multi_line" and x_kind not in TEMPORAL_KINDS:
        return _no("x_not_temporal")
    if any(_category_text(r[sp]) is None for r in rows):
        return _no("missing_value")
    series_values = {_category_text(r[sp]) for r in rows}
    x_values = {(cell_text(r[xp]) if x_kind not in ("boolean",) else _category_text(r[xp])) for r in rows}
    if display in ("grouped_bar", "multi_line", "heatmap"):
        if any(getattr(r[xp], "value", None) is None for r in rows):
            return _no("missing_value")
    if display == "multi_line" and any(not _valid_time(r[xp]) for r in rows):
        return _no("invalid_time")
    limit_series = {
        "grouped_bar": MAX_GROUPED_SERIES, "multi_line": MAX_MULTI_LINE_SERIES,
        "heatmap": MAX_HEATMAP_SIDE, "pivot": MAX_PIVOT_SERIES,
    }[display]
    if len(series_values) > limit_series:
        return _no("too_many_series")
    if display == "grouped_bar" and len(x_values) > MAX_GROUPED_X:
        return _no("too_many_rows")
    if display == "heatmap" and len(x_values) > MAX_HEATMAP_SIDE:
        return _no("too_many_rows")
    pairs: set[tuple[Any, Any]] = set()
    for row in rows:
        pair = (_pair_x(row[xp], x_kind), _category_text(row[sp]))
        if pair in pairs:
            return _no("duplicate_pair")
        pairs.add(pair)
    if display == "pivot":
        return OK
    return _plottable(values(value_position))


def _pair_x(cell: Any, kind: str | None) -> Any:
    if kind == "boolean":
        return _category_text(cell)
    return cell_text(cell) if getattr(cell, "value", None) is not None else None
