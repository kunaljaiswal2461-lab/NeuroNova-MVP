"""SPEC-08 L1-8: output-intent checks are warnings, never errors."""
from __future__ import annotations

import pytest

from app.services.nl_sql import OutputIntent, intent_warnings

COLS = [("month", "timestamp with time zone"), ("day", "date"), ("revenue", "numeric"), ("region", "text"), ("orders", "int8")]


def intent(type_: str, **enc) -> OutputIntent:
    base = {"x": None, "y": [], "series": None, "label": None, "value": None}
    return OutputIntent.model_validate({"type": type_, "encodings": {**base, **enc}})


def test_clean_line_chart():
    assert intent_warnings(intent("line", x="month", y=["revenue"]), COLS) == ()


def test_clean_kpi_and_bar_and_pie():
    assert intent_warnings(intent("kpi", value="orders"), COLS) == ()
    assert intent_warnings(intent("bar", x="region", y=["revenue"]), COLS) == ()
    assert intent_warnings(intent("pie", label="region", y=["revenue"]), COLS) == ()


def test_date_and_timestamp_both_count_as_time():
    assert intent_warnings(intent("area", x="day", y=["revenue"]), COLS) == ()
    assert intent_warnings(intent("multi_line", x="month", y=["revenue", "orders"]), COLS) == ()


@pytest.mark.parametrize("field,kwargs", [
    ("x", {"x": "nope"}), ("series", {"series": "nope"}), ("label", {"label": "nope"}), ("value", {"value": "nope"}),
    ("y", {"y": ["revenue", "nope"]}),
])
def test_unknown_label_in_every_slot(field, kwargs):
    assert f"intent_warning:unknown_label:{field}" in intent_warnings(intent("table", **kwargs), COLS)


@pytest.mark.parametrize("chart", ["line", "area", "multi_line"])
def test_time_charts_need_a_temporal_x(chart):
    assert intent_warnings(intent(chart, x="region", y=["revenue"]), COLS) == ("intent_warning:time_axis_not_temporal",)
    assert intent_warnings(intent(chart, y=["revenue"]), COLS) == ("intent_warning:time_axis_not_temporal",)


def test_pie_with_two_measures():
    assert intent_warnings(intent("pie", label="region", y=["revenue", "orders"]), COLS) == ("intent_warning:pie_multiple_y",)


def test_no_intent_no_warnings():
    assert intent_warnings(None, COLS) == ()
