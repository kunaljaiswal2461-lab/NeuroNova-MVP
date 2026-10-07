"""SPEC-03 §16.6 — B3 (version refusal) and R5 end-to-end with a fake candidate, no database."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
import yaml

from evals import report as rpt
from evals.candidates.base import CandidateOutcome, EvalContext, LLMCall
from evals.grader import ResultColumn, ResultSet, Verdict
from evals.models import GoldenQuestion
from evals.runner import RunnerRefusal, evaluate, load_candidate, preflight

META = {
    "fixture_version": 1,
    "reference_clock": "2026-06-15T10:00:00+05:30",
    "reference_time_zone": "Asia/Kolkata",
    "tags": ["aggregate"],
    "output_types": ["kpi"],
}


def _golden(tmp_path, *, meta_version=1, manifest_version=1):
    g = tmp_path / "golden"
    (g / "questions").mkdir(parents=True)
    (g / "adversarial").mkdir()
    (g / "meta.yaml").write_text(yaml.safe_dump({**META, "fixture_version": meta_version}))
    (g / "questions" / "a.yaml").write_text("[]")
    (g / "adversarial" / "s.yaml").write_text("[]")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"fixture_version": manifest_version}))
    return g, manifest


def test_b3_runner_refuses_on_fixture_version_mismatch(tmp_path):
    g, m = _golden(tmp_path, meta_version=2, manifest_version=1)
    with pytest.raises(RunnerRefusal, match="fixture v2"):
        preflight(g, m)


def test_b3_runner_accepts_matching_versions(tmp_path):
    g, m = _golden(tmp_path)
    assert preflight(g, m).fixture_version == 1


def test_runner_refuses_on_lint_errors(tmp_path):
    g, m = _golden(tmp_path)
    bad = {"id": "x", "question": "q", "tags": ["nope"], "expect": {"kind": "refusal", "category": "write"}}
    (g / "questions" / "a.yaml").write_text(yaml.safe_dump([bad]))
    with pytest.raises(RunnerRefusal, match="lint error"):
        preflight(g, m)


def test_unknown_candidate_is_refused():
    with pytest.raises(RunnerRefusal):
        load_candidate("nonsense")


def _q(qid: str) -> GoldenQuestion:
    return GoldenQuestion.model_validate(
        {"id": qid, "question": "q", "tags": ["aggregate"],
         "expect": {"kind": "answer", "reference_sql": "SELECT 1", "output_type": "kpi"}}
    )


def _rs(n) -> ResultSet:
    return ResultSet((ResultColumn("n", "int8"),), ((n,),))


class Alternating:
    """Right on even runs, wrong on odd runs, for questions whose id ends in 'f'."""

    name = "alternating"

    def __init__(self):
        self.calls: dict[str, int] = {}

    async def answer(self, question, ctx):
        n = self.calls[question.id] = self.calls.get(question.id, 0) + 1
        right = question.id.endswith("s") or n % 2 == 1
        return CandidateOutcome(
            kind="answer", result=_rs(1 if right else 2), sql="SELECT 1", latency_ms=10.0 * n,
            llm_calls=(LLMCall("m1", 100, 20, 5.0),),
        )


class Crashing:
    name = "crashing"

    async def answer(self, question, ctx):
        raise RuntimeError("secret internal detail that must not reach the report")


CTX = EvalContext(fixture=None, grader_connect=None, reference_clock="x", reference_time_zone="y")


def _run(candidate, ids, repeat=3):
    qs = [_q(i) for i in ids]
    refs = {q.id: _rs(1) for q in qs}
    return asyncio.run(evaluate(qs, candidate, CTX, repeat=repeat, concurrency=2, refs=refs, alts={}))


def test_r5_repeat_three_with_alternating_candidate():
    records = _run(Alternating(), ["steady-s", "flaky1-f", "flaky2-f"])
    assert len(records) == 9
    agg = rpt.aggregate(records, pricing={})
    assert agg.consistency == pytest.approx(1 / 3)
    assert agg.accuracy.p == pytest.approx((3 + 2 + 2) / 9)
    assert agg.accuracy.n == 3
    assert agg.input_tokens == 900 and agg.output_tokens == 180
    assert [r.run_index for r in records if r.question_id == "flaky1-f"] == [0, 1, 2]


def test_a_crashing_candidate_is_a_failed_run_with_a_safe_code():
    records = _run(Crashing(), ["q1"], repeat=1)
    assert records[0].verdict is Verdict.FAIL_ERROR
    assert records[0].reason == "error:candidate_exception:RuntimeError"
    assert "secret" not in records[0].model_dump_json()
