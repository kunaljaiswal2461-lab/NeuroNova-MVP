"""SPEC-05 follow-up: non-finite numerics and infinite dates are strings, like floats already are."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.postgres_query_compiler import ExpectedPostgresOutputColumn
from app.services.postgres_query_executor import _ResultSerializationError, _serialize_value


def col(pg_type: str) -> ExpectedPostgresOutputColumn:
    return ExpectedPostgresOutputColumn(alias="x", logical_type=pg_type, postgres_type=pg_type,
                                        source_column_id=None, source_column_name=None)


@pytest.mark.parametrize("value,text", [(Decimal("NaN"), "NaN"), (Decimal("Infinity"), "Infinity"),
                                        (Decimal("-Infinity"), "-Infinity"), (float("inf"), "Infinity"),
                                        (float("nan"), "NaN")])
def test_non_finite_numbers(value, text):
    pg = "float8" if isinstance(value, float) else "numeric"
    out = _serialize_value(value, col(pg))
    assert (out.kind, out.value) == ("non_finite_number", text)


def test_finite_numeric_is_unchanged():
    out = _serialize_value(Decimal("12.3400"), col("numeric"))
    assert (out.kind, out.value) == ("decimal", "12.3400")


@pytest.mark.parametrize("pg,text", [
    ("date", "infinity"), ("date", "-infinity"), ("timestamp", "infinity"), ("timestamp", "-infinity"),
    ("timestamptz", "infinity"), ("timestamptz", "-infinity"),
])
def test_infinite_values_arrive_as_text_and_pass_through(pg, text):
    out = _serialize_value(text, col(pg))
    assert (out.kind, out.value) == (pg, text)


@pytest.mark.parametrize("pg,text,expected", [
    ("date", "9999-12-31", "9999-12-31"),
    ("date", "0044-03-15 BC", "0044-03-15 BC"),
    ("date", "275760-09-13", "275760-09-13"),
    ("timestamp", "2026-01-02 03:04:05", "2026-01-02T03:04:05"),
    ("timestamp", "2026-01-02 03:04:05.5", "2026-01-02T03:04:05.500000"),
    ("timestamp", "9999-12-31 23:59:59.999999", "9999-12-31T23:59:59.999999"),
    ("timestamp", "0044-03-15 01:02:03 BC", "0044-03-15 01:02:03 BC"),
    ("timestamptz", "2026-01-02 03:04:05+00", "2026-01-02T03:04:05Z"),
    ("timestamptz", "2026-01-02 03:04:05.25+00", "2026-01-02T03:04:05.250000Z"),
    ("timestamptz", "9999-12-31 23:59:59+00", "9999-12-31T23:59:59Z"),
    ("timestamptz", "12345-01-02 03:04:05+00", "12345-01-02 03:04:05+00"),
    ("timestamptz", "0044-03-15 01:02:03.5+00 BC", "0044-03-15 01:02:03.5+00 BC"),
])
def test_real_extreme_values_stay_distinct_from_infinity(pg, text, expected):
    out = _serialize_value(text, col(pg))
    assert (out.kind, out.value) == (pg, expected)
    assert out.value not in {"infinity", "-infinity"}


@pytest.mark.parametrize("pg,text", [
    ("date", "01/02/2026"), ("date", "2 Jan 2026"), ("timestamp", "2026-01-02T03:04:05"),
    ("timestamp", "Jan 2 2026"),
    ("timestamptz", "2026-01-02 03:04:05"), ("date", "garbage"),
])
def test_text_outside_the_pinned_iso_form_is_refused(pg, text):
    with pytest.raises(_ResultSerializationError):
        _serialize_value(text, col(pg))


def test_date_objects_mean_what_they_say():
    # A real 9999-12-31 object is NOT infinity any more (the adapter hands over text for infinities).
    assert _serialize_value(date.max, col("date")).value == "9999-12-31"
    assert _serialize_value(datetime.max.replace(tzinfo=timezone.utc), col("timestamptz")).value.startswith("9999-12-31")


def test_ordinary_dates_are_unchanged():
    assert _serialize_value(date(2026, 1, 2), col("date")).value == "2026-01-02"
    assert _serialize_value(datetime(2026, 1, 2, 3, 4, 5), col("timestamp")).value == "2026-01-02T03:04:05"
    assert _serialize_value(datetime(9999, 12, 31, 23, 59, 59), col("timestamp")).value == "9999-12-31T23:59:59"
    assert _serialize_value(datetime(2026, 1, 2, tzinfo=timezone.utc), col("timestamptz")).value == "2026-01-02T00:00:00Z"


def test_wrong_python_types_still_fail():
    for pg, bad in (("numeric", 1.5), ("date", 5), ("timestamptz", datetime(2026, 1, 1))):
        with pytest.raises(_ResultSerializationError):
            _serialize_value(bad, col(pg))


def test_an_ordinary_naive_timestamptz_is_still_an_error():
    with pytest.raises(_ResultSerializationError):
        _serialize_value(datetime(2026, 1, 1), col("timestamptz"))
