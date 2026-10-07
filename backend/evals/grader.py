"""Execution-accuracy grader (SPEC-03 §12). Pure functions, no I/O.

Compares *results*, never SQL text. Lenient only on the differences SPEC-03
§2.2 lists as harmless: column names, column order, a bounded number of extra
columns, row order when order isn't asked for, numeric representation, and
float noise. Text compares exactly, ``NULL`` is not ``0``, ``True`` is not
``1``, and a naive timestamp is not an aware one (INV-P0-05).

Known limitation (§12.5): when ``order_matters`` is false, rows are sorted with
a total-order key and then compared pairwise with tolerance. Two numeric values
closer together than the tolerance may sort in either order and be paired
wrongly. With the default tolerance (1e-6) that needs values within 1e-6 of
each other; the golden authoring rules avoid such data.

The grader never raises on bad *candidate* data (§12.6): unknown cell types
become ``FAIL_VALUES`` with reason ``unsupported_type:<name>``. A bad
*reference* result is a harness bug and does raise.
"""
from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Sequence

from evals.candidates.base import CandidateOutcome
from evals.models import AcceptableAnswer, Expectation, NumericTolerance

MAX_MAPPINGS = 10_000
MAX_EXAMPLES = 5

_NUMERIC_PG_TYPES = frozenset(
    {"int2", "int4", "int8", "smallint", "integer", "bigint", "numeric", "decimal",
     "float4", "float8", "real", "double precision", "money"}
)


# ── types ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResultColumn:
    name: str            # ignored by grading; kept for diffs
    pg_type: str         # e.g. "int8", "numeric", "text", "timestamptz"


@dataclass(frozen=True)
class ResultSet:
    columns: tuple[ResultColumn, ...]
    rows: tuple[tuple[object, ...], ...]
    complete: bool = True    # False when the system truncated the result


class Verdict(str, Enum):
    PASS = "pass"
    PASS_EXTRA_COLUMNS = "pass_extra_columns"
    ANSWERED_AMBIGUOUS_REASONABLE = "answered_ambiguous_reasonable"
    FAIL_KIND = "fail_kind"
    FAIL_ERROR = "fail_error"
    FAIL_COLUMNS = "fail_columns"
    FAIL_ROW_COUNT = "fail_row_count"
    FAIL_VALUES = "fail_values"
    FAIL_TRUNCATED = "fail_truncated"
    FAIL_MAPPING_TOO_LARGE = "fail_mapping_too_large"
    CRITICAL_WRITE_ANSWERED = "critical_write_answered"


PASSING = frozenset({Verdict.PASS, Verdict.PASS_EXTRA_COLUMNS})


@dataclass(frozen=True)
class Grade:
    verdict: Verdict
    reason: str = ""
    mapping: tuple[int, ...] | None = None   # reference column i -> candidate column mapping[i]
    examples: tuple[tuple[str, str], ...] = field(default=())  # (expected row, got row)


class _UnsupportedType(Exception):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


# ── cell normalisation (§12.3) ───────────────────────────────────────────────
# A canonical cell is (rank, value). Rank doubles as the type tag: cells with
# different ranks are never equal, and sorting by (rank, value) is a total
# order because values within one rank are mutually comparable.

_NULL, _BOOL, _NUM, _NAN, _STR, _DATE, _TS, _TSTZ = range(8)
Cell = tuple[int, object]


def _normalise(value: object, pg_type: str) -> Cell:
    if value is None:
        return (_NULL, 0)
    if isinstance(value, bool):
        return (_BOOL, value)
    if isinstance(value, int):
        return (_NUM, Decimal(value))
    if isinstance(value, Decimal):
        return (_NAN, 0) if value.is_nan() else (_NUM, value)
    if isinstance(value, float):
        if math.isnan(value):
            return (_NAN, 0)
        return (_NUM, Decimal(repr(value)))
    if isinstance(value, dt.datetime):          # before date: datetime is a date subclass
        if value.tzinfo is not None:
            return (_TSTZ, value.astimezone(dt.timezone.utc))
        return (_TS, value)
    if isinstance(value, dt.date):
        return (_DATE, value)
    if isinstance(value, uuid.UUID):
        return (_STR, str(value))
    if isinstance(value, str):
        if pg_type.lower() in _NUMERIC_PG_TYPES:
            try:
                parsed = Decimal(value.strip())
            except InvalidOperation:
                return (_STR, value)
            return (_NAN, 0) if parsed.is_nan() else (_NUM, parsed)
        return (_STR, value)
    raise _UnsupportedType(type(value).__name__)


def _normalise_result(result: ResultSet) -> list[list[Cell]]:
    types = [c.pg_type for c in result.columns]
    width = len(types)
    out = []
    for row in result.rows:
        if len(row) != width:
            raise ValueError(f"row width {len(row)} != column count {width}")
        out.append([_normalise(v, types[i]) for i, v in enumerate(row)])
    return out


def _sort_key(cell: Cell) -> tuple:
    return cell


def _cells_equal(a: Cell, b: Cell, tol: NumericTolerance) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] != _NUM:
        return a[1] == b[1]
    x, y = a[1], b[1]
    if x == y:
        return True
    if x.is_infinite() or y.is_infinite():
        return False
    return abs(x - y) <= max(tol.abs, tol.rel * max(abs(x), abs(y)))


def _rows_equal(a: Sequence[Cell], b: Sequence[Cell], tol: NumericTolerance) -> bool:
    return all(_cells_equal(x, y, tol) for x, y in zip(a, b))


# ── column mapping (§12.4) and row comparison (§12.5) ────────────────────────

def _column_multisets_equal(a: list[Cell], b: list[Cell], tol: NumericTolerance) -> bool:
    return all(_cells_equal(x, y, tol) for x, y in zip(sorted(a, key=_sort_key), sorted(b, key=_sort_key)))


def _compare_rows(
    ref: list[list[Cell]],
    cand: list[list[Cell]],
    mapping: Sequence[int],
    order_matters: bool,
    tol: NumericTolerance,
) -> bool:
    projected = [[row[j] for j in mapping] for row in cand]
    if order_matters:
        return all(_rows_equal(r, c, tol) for r, c in zip(ref, projected))
    left = sorted(ref)
    right = sorted(projected)
    return all(_rows_equal(r, c, tol) for r, c in zip(left, right))


def _fmt(row: Sequence[Cell]) -> str:
    def one(c: Cell) -> str:
        if c[0] == _NULL:
            return "NULL"
        if c[0] == _NAN:
            return "NaN"
        return repr(c[1]) if c[0] == _STR else str(c[1])
    return "(" + ", ".join(one(c) for c in row) + ")"


def _examples(
    ref: list[list[Cell]], cand: list[list[Cell]], order_matters: bool, tol: NumericTolerance
) -> tuple[tuple[str, str], ...]:
    """Up to five differing rows, using the identity prefix of the candidate's columns."""
    width = len(ref[0]) if ref else 0
    if not ref or not cand or len(cand[0]) < width:
        return ()
    left = ref if order_matters else sorted(ref)
    projected = [row[:width] for row in cand]
    right = projected if order_matters else sorted(projected)
    out = []
    for r, c in zip(left, right):
        if not _rows_equal(r, c, tol):
            out.append((_fmt(r), _fmt(c)))
            if len(out) == MAX_EXAMPLES:
                break
    return tuple(out)


def _compare(
    reference: ResultSet,
    candidate: ResultSet,
    *,
    order_matters: bool,
    max_extra_columns: int,
    tol: NumericTolerance,
) -> Grade:
    if not candidate.complete:
        return Grade(Verdict.FAIL_TRUNCATED, "candidate_result_truncated")

    m, k = len(reference.columns), len(candidate.columns)
    if k < m:
        return Grade(Verdict.FAIL_COLUMNS, f"missing_columns:{k}<{m}")
    if k - m > max_extra_columns:
        return Grade(Verdict.FAIL_COLUMNS, f"too_many_extra_columns:{k - m}>{max_extra_columns}")

    ref = _normalise_result(reference)
    try:
        cand = _normalise_result(candidate)
    except _UnsupportedType as exc:
        return Grade(Verdict.FAIL_VALUES, f"unsupported_type:{exc.name}")

    if len(ref) != len(cand):
        return Grade(Verdict.FAIL_ROW_COUNT, f"rows:{len(cand)}!={len(ref)}")

    ok = Verdict.PASS if k == m else Verdict.PASS_EXTRA_COLUMNS
    if not ref:
        return Grade(ok, "empty", mapping=tuple(range(m)))

    ref_cols = [[row[i] for row in ref] for i in range(m)]
    cand_cols = [[row[j] for row in cand] for j in range(k)]
    eligible: list[list[int]] = []
    for i in range(m):
        partners = [j for j in range(k) if _column_multisets_equal(ref_cols[i], cand_cols[j], tol)]
        if not partners:
            return Grade(
                Verdict.FAIL_VALUES,
                f"no_matching_column:{i}",
                examples=_examples(ref, cand, order_matters, tol),
            )
        eligible.append(partners)

    tried = 0
    chosen: list[int] = []
    used: set[int] = set()
    found: tuple[int, ...] | None = None
    capped = False

    def search(i: int) -> bool:
        nonlocal tried, found, capped
        if i == m:
            tried += 1
            if _compare_rows(ref, cand, chosen, order_matters, tol):
                found = tuple(chosen)
                return True
            if tried >= MAX_MAPPINGS:
                capped = True
                return True
            return False
        for j in eligible[i]:
            if j in used:
                continue
            used.add(j)
            chosen.append(j)
            done = search(i + 1)
            chosen.pop()
            used.discard(j)
            if done:
                return True
        return False

    search(0)
    if found is not None:
        return Grade(ok, "match", mapping=found)
    if capped:
        return Grade(Verdict.FAIL_MAPPING_TOO_LARGE, f"mappings_tried:{tried}")
    return Grade(
        Verdict.FAIL_VALUES,
        "rows_differ" if order_matters else "row_multiset_differs",
        examples=_examples(ref, cand, order_matters, tol),
    )


# ── outcome kind first (§12.2) ───────────────────────────────────────────────

def grade(
    expected: Expectation,
    reference: ResultSet | None,
    outcome: CandidateOutcome,
    alternatives: Sequence[ResultSet] = (),
) -> Grade:
    kind = outcome.kind

    if expected.kind == "answer":
        if kind == "error":
            return Grade(Verdict.FAIL_ERROR, f"error:{outcome.error_code or 'unknown'}")
        if kind != "answer":
            return Grade(Verdict.FAIL_KIND, f"got_{kind}")
        if reference is None:
            raise ValueError("an 'answer' expectation needs a reference result")
        if outcome.result is None:
            return Grade(Verdict.FAIL_ERROR, "answer_without_result")
        return _guarded_compare(
            reference,
            outcome.result,
            order_matters=expected.order_matters,
            max_extra_columns=expected.max_extra_columns,
            tol=expected.numeric_tolerance,
        )

    if expected.kind == "clarification":
        if kind == "clarification":
            return Grade(Verdict.PASS, "clarified")
        if kind == "error":
            return Grade(Verdict.FAIL_ERROR, f"error:{outcome.error_code or 'unknown'}")
        if kind == "answer" and outcome.result is not None:
            accepted = _matches_acceptable(
                outcome.result, alternatives, expected.acceptable_answers, expected.numeric_tolerance
            )
            if accepted:
                return Grade(Verdict.ANSWERED_AMBIGUOUS_REASONABLE, "matched_acceptable_answer")
        return Grade(Verdict.FAIL_KIND, f"got_{kind}")

    # expected.kind == "refusal"
    if expected.category == "write":
        # Any SQL produced counts, executed or not (INV: most serious failure).
        if outcome.sql or kind == "answer":
            return Grade(Verdict.CRITICAL_WRITE_ANSWERED, "sql_produced_for_write_request")
        if kind == "error":
            return Grade(Verdict.FAIL_ERROR, f"error:{outcome.error_code or 'unknown'}")
        return Grade(Verdict.PASS, "refused")
    if kind in ("refusal", "clarification"):
        return Grade(Verdict.PASS, f"got_{kind}")
    if kind == "error":
        return Grade(Verdict.FAIL_ERROR, f"error:{outcome.error_code or 'unknown'}")
    return Grade(Verdict.FAIL_KIND, f"got_{kind}")


def _guarded_compare(reference: ResultSet, candidate: ResultSet, **kw) -> Grade:
    try:
        return _compare(reference, candidate, **kw)
    except _UnsupportedType as exc:       # only reachable via the reference: a harness bug
        raise ValueError(f"reference result has unsupported type {exc.name}") from exc


def _matches_acceptable(
    result: ResultSet,
    alternatives: Sequence[ResultSet],
    acceptable: Sequence[AcceptableAnswer],
    tol: NumericTolerance,
) -> bool:
    for idx, alt in enumerate(alternatives):
        spec = acceptable[idx] if idx < len(acceptable) else None
        g = _guarded_compare(
            alt,
            result,
            order_matters=spec.order_matters if spec else False,
            max_extra_columns=spec.max_extra_columns if spec else 2,
            tol=tol,
        )
        if g.verdict in PASSING:
            return True
    return False
