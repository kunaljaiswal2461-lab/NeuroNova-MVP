"""SPEC-07 L1 items 3 and 4: selection math and value rules."""
from __future__ import annotations

import math

import pytest

from app.services.sample_values import select as sel
from app.services.sample_values.select import (
    decide_from_exact,
    decide_from_pg_stats,
    estimated_distinct,
    finalize_values,
)
from app.services.schema_context import literals


def test_limits_match_part_3():
    assert sel.SAMPLE_VALUE_LIMIT == literals.SAMPLE_VALUE_LIMIT == 64
    assert sel.MAX_SAMPLE_VALUES == literals.MAX_SAMPLE_VALUES == 20


# ── estimated distinct count ─────────────────────────────────────────────────────────────────────────
def test_positive_n_distinct_is_a_count():
    assert estimated_distinct(4, 5000) == 4
    assert estimated_distinct(4, -1) == 4                 # row count is irrelevant when n_distinct is a count
    assert estimated_distinct(0, 5000) == 0


def test_negative_n_distinct_is_a_fraction_of_reltuples():
    assert estimated_distinct(-0.5, 100) == 50
    assert estimated_distinct(-1, 36) == 36               # regions.name: all distinct
    assert estimated_distinct(-0.009, 400) == pytest.approx(3.6)


@pytest.mark.parametrize("reltuples", [-1, None, math.nan, math.inf])
def test_negative_n_distinct_without_a_usable_row_count_is_unknown(reltuples):
    assert estimated_distinct(-0.5, reltuples) is None


@pytest.mark.parametrize("n", [None, math.nan, True])
def test_unusable_n_distinct_is_unknown(n):
    assert estimated_distinct(n, 100) is None


# ── pg_stats decision ────────────────────────────────────────────────────────────────────────────────
def _stats(**kw):
    base = dict(n_distinct=4, null_frac=0.0, vals=["a", "b"], freqs=[0.6, 0.4], reltuples=1000,
                max_distinct=50, min_coverage=0.95)
    base.update(kw)
    return decide_from_pg_stats(**base)


def test_complete_list_is_kept_in_frequency_order():
    out = _stats(vals=["b", "a"], freqs=[0.4, 0.6])
    assert out.skip is None and out.method == "pg_stats"
    assert [v for v, _ in out.values] == ["a", "b"]
    assert out.coverage == pytest.approx(1.0)


def test_fifty_distinct_boundary():
    assert _stats(n_distinct=50).skip is None
    assert _stats(n_distinct=51).skip == "high_cardinality"
    assert _stats(n_distinct=-0.05, reltuples=1000).skip is None           # 50.0
    assert _stats(n_distinct=-0.051, reltuples=1000).skip == "high_cardinality"


def test_high_cardinality_wins_over_low_coverage():
    assert _stats(n_distinct=500, vals=["a"], freqs=[0.01]).skip == "high_cardinality"


def test_coverage_boundary_at_95_percent():
    assert _stats(freqs=[0.5, 0.449]).skip == "low_coverage"                 # 0.949
    assert _stats(freqs=[0.5, 0.45]).skip is None                            # exactly 0.95
    assert _stats(freqs=[0.5, 0.451]).skip is None                           # 0.951


def test_float4_rounding_does_not_reject_an_exact_95():
    assert _stats(freqs=[0.5, 0.44999994]).skip is None


def test_null_fraction_counts_toward_coverage():
    assert _stats(freqs=[0.5, 0.4], null_frac=0.05).skip is None
    assert _stats(freqs=[0.5, 0.4], null_frac=0.04).skip == "low_coverage"
    assert _stats(freqs=[0.5, 0.4], null_frac=None).skip == "low_coverage"


@pytest.mark.parametrize("vals,freqs", [(None, None), ([], []), (["a"], [])])
def test_empty_or_inconsistent_mcv_list_is_low_coverage(vals, freqs):
    assert _stats(vals=vals, freqs=freqs).skip == "low_coverage"


def test_unknown_distinct_count_is_no_statistics():
    assert _stats(n_distinct=-0.5, reltuples=-1).skip == "no_statistics"
    assert _stats(n_distinct=None).skip == "no_statistics"


def test_the_hostile_refund_reason_is_the_missing_five_hundredths_of_a_percent():
    # refunds.reason: 7 of 8 values, coverage 0.9995. The list is kept; the hostile value is simply absent.
    vals = [f"reason {i}" for i in range(7)]
    out = _stats(n_distinct=8, vals=vals, freqs=[0.142] * 7 + [], null_frac=0.0005)
    assert out.skip is None and len(out.values) == 7


def test_pii_value_drops_the_whole_column_before_the_cap():
    vals = [f"v{i:02d}" for i in range(25)] + ["someone@example.com"]
    freqs = [0.0384] * 26
    out = _stats(n_distinct=26, vals=vals, freqs=freqs)
    assert out.skip == "pii_values" and out.values == ()


# ── exact decision ───────────────────────────────────────────────────────────────────────────────────
def test_exact_groups_are_kept_with_fractions_and_null_is_dropped():
    out = decide_from_exact(groups=[("a", 6), (None, 2), ("B", 2)], max_distinct=50)
    assert out.skip is None and out.method == "exact" and out.coverage == 1.0
    assert out.values == (("a", 0.6), ("B", 0.2))                  # NULL counted in the total, not stored


def test_exact_over_fifty_distinct_is_high_cardinality():
    groups = [(f"v{i}", 1) for i in range(51)]
    assert decide_from_exact(groups=groups, max_distinct=50).skip == "high_cardinality"
    assert decide_from_exact(groups=groups[:50], max_distinct=50).skip is None


def test_exact_null_group_does_not_count_as_a_distinct_value():
    groups = [(f"v{i}", 2) for i in range(50)] + [(None, 1)]
    assert decide_from_exact(groups=groups, max_distinct=50).skip is None


def test_exact_empty_or_all_null_has_nothing_to_show():
    assert decide_from_exact(groups=[], max_distinct=50).skip == "low_coverage"
    assert decide_from_exact(groups=[(None, 9)], max_distinct=50).skip == "low_coverage"


def test_exact_pii_value_skips_the_column():
    assert decide_from_exact(groups=[("a@b.co", 3), ("x", 1)], max_distinct=50).skip == "pii_values"


# ── value rules ──────────────────────────────────────────────────────────────────────────────────────
def test_null_is_dropped():
    assert finalize_values([(None, 0.9), ("a", 0.1)]) == (("a", 0.1),)


def test_order_is_frequency_then_code_point():
    got = finalize_values([("b", 0.2), ("B", 0.2), ("a", 0.5), ("é", 0.2), ("Z", 0.1)])
    assert [v for v, _ in got] == ["a", "B", "b", "é", "Z"]


def test_cap_is_twenty():
    got = finalize_values([(f"v{i:02d}", 1 / 30) for i in range(30)])
    assert len(got) == 20 and got[0][0] == "v00" and got[-1][0] == "v19"


def test_long_values_keep_one_code_point_past_the_render_limit():
    long = "x" * 200
    (v, _), = finalize_values([(long, 1.0)])
    assert len(v) == 65
    # Part 3 then renders it as 63 characters and an ellipsis, which is how the cut is marked.
    rendered = literals.untrusted_literal(v, limit=literals.SAMPLE_VALUE_LIMIT)
    assert rendered == '"' + "x" * 63 + '…"'


def test_exactly_64_code_points_is_not_marked_as_cut():
    (v, _), = finalize_values([("y" * 64, 1.0)])
    assert literals.untrusted_literal(v, limit=64) == '"' + "y" * 64 + '"'


def test_truncation_counts_code_points_not_bytes_or_utf16_units():
    (v, _), = finalize_values([("😀" * 100, 1.0)])
    assert v == "😀" * 65


def test_case_and_trailing_space_are_kept_byte_exact():
    got = finalize_values([("Enterprise", 0.2), ("enterprise ", 0.0024), ("SMB", 0.3)])
    assert "enterprise " in [v for v, _ in got]
    assert [v for v, _ in got if v.lower().startswith("enter")] == ["Enterprise", "enterprise "]
