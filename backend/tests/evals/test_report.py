"""SPEC-03 §16.6 — R3–R6 (report maths, baseline gate, consistency, ambiguity exclusion)."""
from __future__ import annotations

import math

import pytest

from evals.grader import Verdict
from evals.report import (
    Baseline,
    Report,
    RunMeta,
    RunRecord,
    accuracy_of,
    aggregate,
    baseline_from_report,
    compare_baseline,
    interval_half_width,
    load_baseline,
    render_markdown,
    write_baseline,
)


def rec(qid, verdict, *, tags=("aggregate",), kind="answer", run=0, **kw) -> RunRecord:
    return RunRecord(
        question_id=qid, tags=list(tags), expected_kind=kind, run_index=run,
        verdict=verdict, **kw,
    )


def report_of(records, candidate="stage4c", **extras) -> Report:
    meta = RunMeta(
        git_commit=None, fixture_version=1, candidate=candidate, suite="golden", models=[],
        repeat=1, started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:01:00Z", extras=extras,
    )
    return Report(meta=meta, aggregates=aggregate(records, pricing={}), records=records)


def n_records(passed: int, total: int, prefix="q", **kw) -> list[RunRecord]:
    return [
        rec(f"{prefix}{i}", Verdict.PASS if i < passed else Verdict.FAIL_VALUES, **kw)
        for i in range(total)
    ]


# R3 ---------------------------------------------------------------------------
def test_r3_interval_matches_hand_computation():
    # p = 0.8, n = 150  ->  SE = sqrt(0.16/150) = 0.032660, half = 1.96 * SE = 0.064013
    assert interval_half_width(0.8, 150) == pytest.approx(1.96 * math.sqrt(0.16 / 150))
    assert interval_half_width(0.8, 150) == pytest.approx(0.0640, abs=1e-4)
    assert interval_half_width(1.0, 150) == 0.0
    assert interval_half_width(0.5, 0) == 0.0


def test_r3_accuracy_from_records():
    acc = accuracy_of(n_records(120, 150))
    assert acc.p == pytest.approx(0.8)
    assert acc.n == 150 and acc.runs == 150
    assert acc.half_interval == pytest.approx(0.0640, abs=1e-4)


def test_r3_report_json_round_trips_through_its_schema():
    r = report_of(n_records(8, 10))
    assert Report.model_validate_json(r.model_dump_json()) == r


def test_pass_extra_columns_counts_as_a_pass():
    recs = [rec("a", Verdict.PASS), rec("b", Verdict.PASS_EXTRA_COLUMNS), rec("c", Verdict.FAIL_VALUES), rec("d", Verdict.FAIL_KIND)]
    assert accuracy_of(recs).p == 0.5


# R6 ---------------------------------------------------------------------------
def test_r6_ambiguous_answers_leave_the_denominator():
    recs = [
        rec("a", Verdict.PASS),
        rec("b", Verdict.FAIL_VALUES),
        rec("c", Verdict.ANSWERED_AMBIGUOUS_REASONABLE, kind="clarification"),
        rec("d", Verdict.ANSWERED_AMBIGUOUS_REASONABLE, kind="clarification"),
    ]
    acc = accuracy_of(recs)
    assert acc.runs == 2 and acc.p == 0.5
    agg = aggregate(recs, pricing={})
    assert agg.ambiguous_answered_rate == 1.0              # both clarification-expected runs guessed


# R5 ---------------------------------------------------------------------------
def test_r5_consistency_with_an_alternating_candidate():
    recs = []
    for q in ("steady", "flaky", "flaky2"):
        for run in range(3):
            if q == "steady":
                verdict = Verdict.PASS
            else:
                verdict = Verdict.PASS if run % 2 == 0 else Verdict.FAIL_VALUES
            recs.append(rec(q, verdict, run=run))
    agg = aggregate(recs, pricing={})
    assert agg.consistency == pytest.approx(1 / 3)
    assert agg.accuracy.runs == 9
    assert agg.accuracy.n == 3                              # k repeats do not inflate n
    assert agg.accuracy.p == pytest.approx((3 + 2 + 2) / 9)


def test_consistency_is_none_for_single_runs():
    assert aggregate(n_records(1, 2), pricing={}).consistency is None


# R4 ---------------------------------------------------------------------------
def baseline_at(p_pass: int, total: int) -> Baseline:
    return baseline_from_report(report_of(n_records(p_pass, total)))


def test_r4_critical_always_blocks():
    recs = n_records(10, 10) + [rec("w", Verdict.CRITICAL_WRITE_ANSWERED, kind="refusal", tags=("write_attempt",))]
    code, msgs = compare_baseline(report_of(recs), baseline_at(10, 10))
    assert code == 1 and any("CRITICAL" in m for m in msgs)


def test_r4_drop_within_interval_passes_beyond_fails():
    base = baseline_at(120, 150)                            # 80% ± 6.4
    within = report_of(n_records(112, 150))                 # 74.7%: -5.3 pts
    beyond = report_of(n_records(105, 150))                 # 70.0%: -10 pts
    assert compare_baseline(within, base)[0] == 0
    code, msgs = compare_baseline(beyond, base)
    assert code == 1 and any("accuracy fell" in m for m in msgs)


def test_r4_improvement_passes():
    assert compare_baseline(report_of(n_records(140, 150)), baseline_at(120, 150))[0] == 0


def test_r4_tag_drops_warn_but_do_not_block():
    base_recs = n_records(15, 15, prefix="a", tags=("window",)) + n_records(50, 60, prefix="b", tags=("aggregate",))
    cur_recs = n_records(12, 15, prefix="a", tags=("window",)) + n_records(50, 60, prefix="b", tags=("aggregate",))
    code, msgs = compare_baseline(report_of(cur_recs), baseline_from_report(report_of(base_recs)))
    assert code == 0
    assert any(m.startswith("warning: tag window") for m in msgs)


def test_r4_missing_baseline_for_model_candidate_blocks():
    code, msgs = compare_baseline(report_of(n_records(5, 5)), None)
    assert code == 1 and any("--write-baseline" in m for m in msgs)


def test_r4_reference_must_be_perfect():
    assert compare_baseline(report_of(n_records(10, 10), candidate="reference"), None)[0] == 0
    assert compare_baseline(report_of(n_records(9, 10), candidate="reference"), None)[0] == 1


def test_r4_mutants_thresholds():
    ok = report_of(n_records(1, 1), candidate="mutants", equivalence_pass_rate=1.0, mutation_kill_rate=0.96)
    low_kill = report_of(n_records(1, 1), candidate="mutants", equivalence_pass_rate=1.0, mutation_kill_rate=0.90)
    bad_eq = report_of(n_records(1, 1), candidate="mutants", equivalence_pass_rate=0.99, mutation_kill_rate=1.0)
    assert compare_baseline(ok, None)[0] == 0
    assert compare_baseline(low_kill, None)[0] == 1
    assert compare_baseline(bad_eq, None)[0] == 1


def test_baseline_file_contains_aggregates_only(tmp_path):
    recs = [rec("q1", Verdict.PASS, sql="SELECT secret FROM x", examples=[("(1)", "(2)")])]
    path = write_baseline(baseline_from_report(report_of(recs)), tmp_path)
    text = path.read_text()
    assert "SELECT" not in text and "secret" not in text and "examples" not in text
    assert load_baseline("stage4c", tmp_path) is not None


def test_markdown_lists_worst_tags_and_failures():
    recs = n_records(1, 4, prefix="w", tags=("window",)) + n_records(4, 4, prefix="a", tags=("aggregate",))
    md = render_markdown(report_of(recs))
    assert "Execution accuracy" in md and "| window |" in md and "fail_values" in md and "w1" in md


def test_cost_is_omitted_without_a_price_entry():
    recs = [rec("q", Verdict.PASS, models=["m1"], input_tokens=1000, output_tokens=500)]
    assert aggregate(recs, pricing={}).estimated_cost_usd is None
    priced = aggregate(recs, pricing={"m1": {"input_per_mtok": 2.0, "output_per_mtok": 8.0}})
    assert priced.estimated_cost_usd == pytest.approx(0.002 + 0.004)
