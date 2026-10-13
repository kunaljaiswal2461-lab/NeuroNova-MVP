"""SPEC-03 §16.2 LT8–LT9 (live) and §16.6 R1–R2. Run only inside the runner container (NN_LIVE_PG=1)."""
from __future__ import annotations

import pytest

from evals import pgrun, report as rpt
from evals.candidates.base import EvalContext
from evals.candidates.reference import ReferenceCandidate
from evals.lint import lint_live, lint_static
from evals.models import GoldenQuestion, load_questions
from evals.report import MUTANT_KILL_TARGET
from evals.runner import evaluate, evaluate_mutants, reference_results

pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio]


def _question(sql: str, *, order_matters: bool) -> GoldenQuestion:
    return GoldenQuestion.model_validate(
        {"id": "t-1", "question": "q", "tags": ["aggregate"],
         "expect": {"kind": "answer", "reference_sql": sql, "order_matters": order_matters}}
    )


async def _lint(question: GoldenQuestion):
    conn = await pgrun.connect_grader()
    try:
        return await lint_live([question], lambda sql: pgrun.run_raw(conn, sql))
    finally:
        await conn.close()


async def test_lt8_tie_at_limit_five_fails_ll3():
    # 60,000 orders over 4 channels: ordering channels' order counts by status gives repeated values.
    sql = "SELECT status FROM shop.orders ORDER BY customer_id IS NULL DESC LIMIT 5"       # 1,856 guests tie
    assert "LL3" in {e.rule for e in await _lint(_question(sql, order_matters=True))}


async def test_lt9_more_than_200_rows_fails_ll4():
    errors = await _lint(_question("SELECT id FROM shop.orders WHERE id <= 201", order_matters=False))
    assert [e.rule for e in errors] == ["LL4"]


async def test_the_committed_golden_set_lints_clean_live():
    static, questions = lint_static()
    assert static == []
    conn = await pgrun.connect_grader()
    try:
        assert await lint_live(questions, lambda sql: pgrun.run_raw(conn, sql)) == []
    finally:
        await conn.close()


def _ctx() -> EvalContext:
    return EvalContext(fixture=None, grader_connect=pgrun.connect_grader, reference_clock="x", reference_time_zone="y")


async def test_r1_reference_scores_100_percent_on_golden_and_adversarial():
    questions = load_questions("all")
    refs, alts = await reference_results(questions, pgrun.connect_grader)
    records = await evaluate(questions, ReferenceCandidate(), _ctx(), repeat=1, concurrency=4, refs=refs, alts=alts)
    failing = [(r.question_id, r.verdict.value, r.reason) for r in records if r.verdict.value not in ("pass", "pass_extra_columns")]
    assert failing == []
    assert rpt.accuracy_of(records).p == 1.0


async def test_r2_mutants_equivalence_100_and_kill_rate_at_target():
    questions = load_questions("golden")
    refs, _ = await reference_results(questions, pgrun.connect_grader)
    _, summary = await evaluate_mutants(questions, refs, pgrun.connect_grader)
    rejected = [(r.question_id, r.kind) for r in summary.results if r.group == "equivalent" and r.status == "fail"]
    assert rejected == [], "the grader rejected an equivalent rewrite: a grader bug"
    assert summary.equivalence_pass_rate == 1.0
    assert summary.mutation_kill_rate >= MUTANT_KILL_TARGET
    assert not [r for r in summary.results if r.status == "invalid"]
    assert not [r for r in summary.survivors if r.survivor_class == "grader_lenient"]
