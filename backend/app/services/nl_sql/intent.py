"""Output-intent checks (SPEC-08 B5): recorded, never fatal. Part 8 decides what to do with the warnings."""
from __future__ import annotations

from collections.abc import Sequence

from .types import OutputIntent

_TIME_AXIS_CHARTS = frozenset({"line", "area", "multi_line"})


def _is_time_type(pg_type: str) -> bool:
    t = pg_type.lower()
    return t == "date" or t.startswith("timestamp")


def intent_warnings(intent: OutputIntent | None, columns: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """``columns`` are the validated query's output ``(label, pg_type)`` pairs."""
    if intent is None:
        return ()
    types = dict(columns)
    enc = intent.encodings
    warnings: list[str] = []
    named = [("x", enc.x), ("series", enc.series), ("label", enc.label), ("value", enc.value)]
    named += [("y", y) for y in enc.y]
    for field, label in named:
        if label is not None and label not in types:
            warnings.append(f"intent_warning:unknown_label:{field}")
    if intent.type in _TIME_AXIS_CHARTS and not (enc.x is not None and _is_time_type(types.get(enc.x, ""))):
        warnings.append("intent_warning:time_axis_not_temporal")
    if intent.type == "pie" and len(enc.y) > 1:
        warnings.append("intent_warning:pie_multiple_y")
    return tuple(warnings)
