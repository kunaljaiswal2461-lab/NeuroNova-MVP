"""SPEC-09 L1-13: the Python C4 implementation against the shared fixture (the same file is used by Vitest)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.nl_sql.display import canonical_decimal, check_display, kind_of, significant_digits

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "display_eligibility.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def rows_of(case):
    return [[SimpleNamespace(**cell) for cell in row] for row in case["rows"]]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case):
    verdict = check_display(
        case["display"], case["encodings"], [tuple(c) for c in case["columns"]], rows_of(case)
    )
    assert (verdict.ok, verdict.reason) == (case["ok"], case["reason"])


def test_the_fixture_covers_every_display_and_enough_cases():
    from app.services.nl_sql.types import OUTPUT_TYPES

    assert len(CASES) >= 120
    shown = {c["display"] for c in CASES}
    assert set(OUTPUT_TYPES) <= shown
    for display in OUTPUT_TYPES:
        verdicts = {c["ok"] for c in CASES if c["display"] == display}
        assert True in verdicts and (display in ("table", "narrative", "takeaways") or False in verdicts)


def test_kind_mapping_is_the_c4_table():
    expected = {
        "int2": "integer", "int4": "integer", "int8": "integer", "numeric": "decimal", "float4": "number",
        "float8": "number", "text": "text", "varchar": "text", "bpchar": "text", "bool": "boolean", "uuid": "uuid",
        "date": "date", "timestamp": "timestamp", "timestamptz": "timestamptz",
    }
    assert {k: kind_of(k) for k in expected} == expected
    assert kind_of("jsonb") is None and kind_of("interval") is None


@pytest.mark.parametrize("text,canonical,digits", [
    ("0", "0", 1), ("0.000", "0", 1), ("100", "1e2", 1), ("1.50", "15e-1", 2), ("-12.5", "-125e-1", 3),
    ("1e3", "1e3", 1), ("1.20E+2", "12e1", 2), (".5", "5e-1", 1), ("007", "7e0", 1), ("123456789012345", "123456789012345e0", 15),
])
def test_canonical_decimal_and_digits(text, canonical, digits):
    assert canonical_decimal(text) == canonical and significant_digits(text) == digits


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "--1", "1e", "NaN", "Infinity"])
def test_canonical_decimal_refuses_non_numbers(bad):
    assert canonical_decimal(bad) is None
