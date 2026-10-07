"""SPEC-03 §16.1 — grader tests G1–G23. Written before the grader."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from evals.candidates.base import CandidateOutcome
from evals.grader import ResultColumn, ResultSet, Verdict, grade
from evals.models import AcceptableAnswer, Expectation, NumericTolerance

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc


def rs(rows, types=None, names=None, complete=True) -> ResultSet:
    rows = tuple(tuple(r) for r in rows)
    width = len(rows[0]) if rows else len(types or names or [])
    types = types or ["text"] * width
    names = names or [f"c{i}" for i in range(len(types))]
    return ResultSet(
        columns=tuple(ResultColumn(n, t) for n, t in zip(names, types)),
        rows=rows,
        complete=complete,
    )


def answer(result: ResultSet) -> CandidateOutcome:
    return CandidateOutcome(kind="answer", result=result, sql="SELECT 1")


def expect(**kw) -> Expectation:
    kw.setdefault("kind", "answer")
    kw.setdefault("reference_sql", "SELECT 1")
    return Expectation(**kw)


def run(ref: ResultSet, cand: ResultSet, **kw):
    return grade(expect(**kw), ref, answer(cand))


# G1–G3 ----------------------------------------------------------------------
def test_g1_identical():
    r = rs([(1, "a"), (2, "b")], ["int8", "text"])
    assert run(r, r).verdict is Verdict.PASS


def test_g2_columns_permuted():
    ref = rs([(1, "a"), (2, "b")], ["int8", "text"])
    cand = rs([("a", 1), ("b", 2)], ["text", "int8"])
    g = run(ref, cand)
    assert g.verdict is Verdict.PASS
    assert g.mapping == (1, 0)


def test_g3_different_names():
    ref = rs([(5,)], ["int8"], names=["count"])
    cand = rs([(5,)], ["int8"], names=["paid_orders"])
    assert run(ref, cand).verdict is Verdict.PASS


# G4–G5 ----------------------------------------------------------------------
def test_g4_extra_columns_limit():
    ref = rs([(1,), (2,)], ["int8"])
    two = rs([(1, "x", 9), (2, "y", 8)], ["int8", "text", "int4"])
    three = rs([(1, "x", 9, True), (2, "y", 8, False)], ["int8", "text", "int4", "bool"])
    assert run(ref, two).verdict is Verdict.PASS_EXTRA_COLUMNS
    assert run(ref, three).verdict is Verdict.FAIL_COLUMNS
    assert run(ref, three, max_extra_columns=3).verdict is Verdict.PASS_EXTRA_COLUMNS


def test_g5_missing_column():
    ref = rs([(1, "a")], ["int8", "text"])
    cand = rs([(1,)], ["int8"])
    assert run(ref, cand).verdict is Verdict.FAIL_COLUMNS


# G6–G7 ----------------------------------------------------------------------
def test_g6_row_order():
    ref = rs([(1,), (2,), (3,)], ["int8"])
    cand = rs([(3,), (1,), (2,)], ["int8"])
    assert run(ref, cand, order_matters=False).verdict is Verdict.PASS
    assert run(ref, cand, order_matters=True).verdict is Verdict.FAIL_VALUES


def test_g7_multiset_not_set():
    ref = rs([("a",), ("a",), ("b",)])
    cand = rs([("a",), ("b",), ("b",)])
    assert run(ref, cand).verdict is Verdict.FAIL_VALUES


# G8–G9 ----------------------------------------------------------------------
def test_g8_int_vs_decimal():
    ref = rs([(5,)], ["int8"])
    cand = rs([(Decimal("5.00"),)], ["numeric"])
    assert run(ref, cand).verdict is Verdict.PASS


def test_g9_float_noise_and_tolerance():
    assert run(rs([(0.3,)], ["float8"]), rs([(0.1 + 0.2,)], ["float8"])).verdict is Verdict.PASS
    ref = rs([(Decimal("100.00"),)], ["numeric"])
    cand = rs([(Decimal("100.02"),)], ["numeric"])
    assert run(ref, cand).verdict is Verdict.FAIL_VALUES
    loose = NumericTolerance(abs=Decimal("0.05"))
    assert run(ref, cand, numeric_tolerance=loose).verdict is Verdict.PASS


# G10–G11 --------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [("PAID", "paid"), ("enterprise", "enterprise ")])
def test_g10_text_is_exact(a, b):
    assert run(rs([(a,)]), rs([(b,)])).verdict is Verdict.FAIL_VALUES


def test_g11_null_semantics():
    assert run(rs([(None,)], ["int8"]), rs([(0,)], ["int8"])).verdict is Verdict.FAIL_VALUES
    assert run(rs([(None,)], ["int8"]), rs([(None,)], ["int8"])).verdict is Verdict.PASS


# G12 ------------------------------------------------------------------------
def test_g12_datetimes():
    t_ist = dt.datetime(2026, 3, 1, 0, 0, tzinfo=IST)
    t_utc = dt.datetime(2026, 2, 28, 18, 30, tzinfo=UTC)
    assert t_ist == t_utc
    assert run(rs([(t_ist,)], ["timestamptz"]), rs([(t_utc,)], ["timestamptz"])).verdict is Verdict.PASS
    naive = dt.datetime(2026, 2, 28, 18, 30)
    assert run(rs([(t_utc,)], ["timestamptz"]), rs([(naive,)], ["timestamp"])).verdict is Verdict.FAIL_VALUES
    d = dt.date(2026, 3, 1)
    midnight = dt.datetime(2026, 3, 1)
    assert run(rs([(d,)], ["date"]), rs([(midnight,)], ["timestamp"])).verdict is Verdict.FAIL_VALUES


# G13 ------------------------------------------------------------------------
def test_g13_empty():
    empty = rs([], ["int8"])
    assert run(empty, empty).verdict is Verdict.PASS
    assert run(empty, rs([(1,)], ["int8"])).verdict is Verdict.FAIL_ROW_COUNT


# G14–G15 --------------------------------------------------------------------
def test_g14_ambiguous_answered():
    alt = rs([("ann", Decimal("10"))], ["text", "numeric"])
    exp = Expectation(
        kind="clarification",
        acceptable_answers=(AcceptableAnswer(reference_sql="SELECT 1"),),
    )
    match = answer(rs([("ann", Decimal("10"))], ["text", "numeric"]))
    other = answer(rs([("bob", Decimal("3"))], ["text", "numeric"]))
    assert grade(exp, None, match, [alt]).verdict is Verdict.ANSWERED_AMBIGUOUS_REASONABLE
    assert grade(exp, None, other, [alt]).verdict is Verdict.FAIL_KIND
    clar = CandidateOutcome(kind="clarification", message="By spend or by orders?")
    assert grade(exp, None, clar, [alt]).verdict is Verdict.PASS


def test_g15_write_refusal_with_unexecuted_sql_is_critical():
    exp = Expectation(kind="refusal", category="write")
    produced = CandidateOutcome(kind="refusal", sql="DELETE FROM shop.orders", message="blocked")
    assert grade(exp, None, produced).verdict is Verdict.CRITICAL_WRITE_ANSWERED
    clean = CandidateOutcome(kind="refusal", message="I can only read data")
    assert grade(exp, None, clean).verdict is Verdict.PASS
    answered = CandidateOutcome(kind="answer", result=rs([(1,)], ["int8"]))
    assert grade(exp, None, answered).verdict is Verdict.CRITICAL_WRITE_ANSWERED


def test_non_write_refusal_answered_is_fail_kind():
    exp = Expectation(kind="refusal", category="unanswerable")
    assert grade(exp, None, answer(rs([(1,)], ["int8"]))).verdict is Verdict.FAIL_KIND
    clar = CandidateOutcome(kind="clarification", message="?")
    assert grade(exp, None, clar).verdict is Verdict.PASS


def test_answer_expected_but_clarified():
    ref = rs([(1,)], ["int8"])
    clar = CandidateOutcome(kind="clarification", message="which?")
    assert grade(expect(), ref, clar).verdict is Verdict.FAIL_KIND


# G16 ------------------------------------------------------------------------
def test_g16_truncated():
    ref = rs([(1,), (2,)], ["int8"])
    cand = rs([(1,), (2,)], ["int8"], complete=False)
    assert run(ref, cand).verdict is Verdict.FAIL_TRUNCATED


# G17–G18 --------------------------------------------------------------------
def test_g17_duplicate_reference_columns():
    ref = rs([(7, 7)], ["int8", "int8"])
    assert run(ref, rs([(7, 7)], ["int8", "int8"])).verdict is Verdict.PASS
    assert run(ref, rs([(7,)], ["int8"])).verdict is Verdict.FAIL_COLUMNS


def test_g18_mapping_cap_is_deterministic():
    n = 12
    ref = rs([(v,) * n for v in (1, 2, 3)], ["int8"] * n)
    # Every candidate column has the same multiset as every reference column,
    # but column 0 is aligned differently, so no mapping can ever verify.
    cand_rows = [(1,) + (1,) * (n - 1), (2,) + (3,) * (n - 1), (3,) + (2,) * (n - 1)]
    cand = rs(cand_rows, ["int8"] * n)
    g1 = run(ref, cand)
    g2 = run(ref, cand)
    assert g1.verdict is Verdict.FAIL_MAPPING_TOO_LARGE
    assert g2.verdict is Verdict.FAIL_MAPPING_TOO_LARGE


# G19 ------------------------------------------------------------------------
def test_g19_error_code_preserved():
    out = CandidateOutcome(kind="error", error_code="validator_rejected")
    g = grade(expect(), rs([(1,)], ["int8"]), out)
    assert g.verdict is Verdict.FAIL_ERROR
    assert "validator_rejected" in g.reason


# G20 ------------------------------------------------------------------------
def test_g20_bool_is_not_int():
    assert run(rs([(True,)], ["bool"]), rs([(1,)], ["int4"])).verdict is Verdict.FAIL_VALUES
    assert run(rs([(1,)], ["int4"]), rs([(True,)], ["bool"])).verdict is Verdict.FAIL_VALUES
    assert run(rs([(True,)], ["bool"]), rs([(True,)], ["bool"])).verdict is Verdict.PASS


# G23 ------------------------------------------------------------------------
def test_g23_unknown_type_does_not_raise():
    class Weird:
        pass

    g = run(rs([(1,)], ["int8"]), rs([(Weird(),)], ["int8"]))
    assert g.verdict is Verdict.FAIL_VALUES
    assert g.reason == "unsupported_type:Weird"


# Extra behaviours the spec describes in prose ---------------------------------
def test_numeric_strings_in_numeric_columns_are_numbers():
    ref = rs([(5,)], ["int8"])
    assert run(ref, rs([("5",)], ["int8"])).verdict is Verdict.PASS


def test_uuid_and_string_forms_match():
    u = uuid.UUID("abcdef12-1234-5678-9abc-def012345678")
    assert run(rs([(u,)], ["uuid"]), rs([(str(u).upper(),)], ["uuid"])).verdict is Verdict.FAIL_VALUES
    assert run(rs([(u,)], ["uuid"]), rs([(str(u),)], ["text"])).verdict is Verdict.PASS


def test_nan_and_infinity():
    nan = float("nan")
    inf = float("inf")
    assert run(rs([(nan,)], ["float8"]), rs([(nan,)], ["float8"])).verdict is Verdict.PASS
    assert run(rs([(inf,)], ["float8"]), rs([(inf,)], ["float8"])).verdict is Verdict.PASS
    assert run(rs([(inf,)], ["float8"]), rs([(1e300,)], ["float8"])).verdict is Verdict.FAIL_VALUES


def test_row_count_mismatch_from_extra_group():
    ref = rs([("a", 1)], ["text", "int8"])
    cand = rs([("a", 1), ("b", 1)], ["text", "int8"])
    assert run(ref, cand).verdict is Verdict.FAIL_ROW_COUNT


def test_failure_carries_example_rows():
    ref = rs([(1,), (2,)], ["int8"])
    cand = rs([(1,), (3,)], ["int8"])
    g = run(ref, cand, order_matters=True)
    assert g.verdict is Verdict.FAIL_VALUES
    assert g.examples and len(g.examples) <= 5


def test_extra_column_mapping_with_ties_between_columns():
    ref = rs([(1, 1), (2, 2)], ["int8", "int8"])
    cand = rs([(1, 9, 1), (2, 8, 2)], ["int8", "int8", "int8"])
    g = run(ref, cand)
    assert g.verdict is Verdict.PASS_EXTRA_COLUMNS


# G21–G22 property tests -------------------------------------------------------
_cell = {
    "int8": st.integers(-10**9, 10**9),
    "text": st.text(alphabet="abcXYZ 0", max_size=5),
    "numeric": st.decimals(min_value=-1000, max_value=1000, places=2, allow_nan=False),
    "bool": st.booleans(),
    "date": st.dates(min_value=dt.date(2024, 1, 1), max_value=dt.date(2026, 12, 31)),
    "timestamptz": st.datetimes(min_value=dt.datetime(2024, 1, 1), max_value=dt.datetime(2026, 12, 31), timezones=st.just(UTC)),
}


@st.composite
def typed_results(draw):
    types = draw(st.lists(st.sampled_from(sorted(_cell)), min_size=1, max_size=4))
    n_rows = draw(st.integers(1, 6))
    rows = [tuple(draw(_cell[t]) for t in types) for _ in range(n_rows)]
    return types, rows


@settings(max_examples=150, deadline=None)
@given(data=typed_results(), seed=st.randoms(use_true_random=False))
def test_g21_permutations_always_pass(data, seed):
    types, rows = data
    perm = list(range(len(types)))
    seed.shuffle(perm)
    shuffled = list(rows)
    seed.shuffle(shuffled)
    cand_rows = [tuple(r[j] for j in perm) for r in shuffled]
    cand_types = [types[j] for j in perm]
    g = run(rs(rows, types), rs(cand_rows, cand_types), order_matters=False)
    assert g.verdict is Verdict.PASS, g


@settings(max_examples=150, deadline=None)
@given(data=typed_results(), pick=st.data())
def test_g22_changed_cell_always_fails(data, pick):
    types, rows = data
    r = pick.draw(st.integers(0, len(rows) - 1))
    c = pick.draw(st.integers(0, len(types) - 1))
    t = types[c]
    old = rows[r][c]
    new = pick.draw(_cell[t].filter(lambda v: _differs(t, old, v)))
    changed = [list(row) for row in rows]
    changed[r][c] = new
    g = run(rs(rows, types), rs(changed, types), order_matters=True)
    assert g.verdict not in (Verdict.PASS, Verdict.PASS_EXTRA_COLUMNS), g


def _differs(t: str, a, b) -> bool:
    if t == "numeric":
        return abs(Decimal(a) - Decimal(b)) > Decimal("0.01")
    return a != b
