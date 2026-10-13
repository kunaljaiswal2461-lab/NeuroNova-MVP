"""SPEC-09 W7.4: the committed scripted stub cassette replays the whole candidate end to end with no key."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from evals import pgrun, report as rpt
from evals.bootstrap import bootstrap
from evals.candidates.base import EvalContext
from evals.candidates.nl_sql import NlSqlCandidate
from evals.candidates.nl_sql_full import NlSqlFullCandidate
from evals.cassette import CassetteLLM
from evals.grader import PASSING, grade
from evals.models import load_meta, load_questions
from evals.runner import evaluate, reference_results

pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio(loop_scope="module")]
CASSETTE = Path(__file__).resolve().parents[1] / "cassettes" / "part9_stub" / "nl_sql_full.jsonl"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ctx():
    fixture = await bootstrap()
    meta = load_meta()
    yield EvalContext(fixture=fixture, grader_connect=pgrun.connect_grader, reference_clock=meta.reference_clock,
                      reference_time_zone=meta.reference_time_zone, cassette_mode="replay", extras={"cassette": "part9_stub", "consent": "off"})
    await fixture.dispose()


async def run_suite(ctx, suite, candidate=None, extras=None):
    questions = load_questions(suite)
    refs, alts = await reference_results(questions, pgrun.connect_grader)
    ctx = EvalContext(**{**ctx.__dict__, "extras": {**ctx.extras, **(extras or {})}})
    return await evaluate(questions, candidate or NlSqlFullCandidate(), ctx, repeat=1, concurrency=1, refs=refs, alts=alts)


async def test_the_stub_cassette_replays_every_suite_without_a_key(ctx):
    assert CASSETTE.exists()
    for suite, floor in (("golden", 0.9), ("adversarial", 1.0), ("chains", 0.9)):
        records = await run_suite(ctx, suite)
        assert not [r for r in records if (r.error_code or "").startswith("candidate_exception")], suite
        assert rpt.accuracy_of(records).p >= floor, suite
        assert not [r for r in records if r.verdict.value == "critical_write_answered"]
        stats = rpt.aggregate(records, {}).pipeline
        if suite == "golden":
            assert stats.repair_by_reason and stats.accuracy_without_repair.p < rpt.accuracy_of(records).p
            assert {"spec_invalid", "number_check_failed"} <= set(stats.pass2_fallbacks)
            assert stats.personal_value_leaks == 0
        if suite == "chains":
            assert stats.chain_accuracy.runs == 20


async def test_repair_off_reproduces_the_part_5_candidate_on_one_cassette(ctx):
    shared = CassetteLLM(CASSETTE, "replay")
    full, part5 = NlSqlFullCandidate(), NlSqlCandidate()
    full._llm = part5._llm = shared
    questions = load_questions("golden")[:40]
    refs, alts = await reference_results(questions, pgrun.connect_grader)
    c = EvalContext(**{**ctx.__dict__, "extras": {"cassette": "part9_stub", "consent": "off", "repair": "off"}})
    for q in questions:
        a = await full.answer(q, c)
        b = await part5.answer(q, c)
        va, vb = (grade(q.expect, refs.get(q.id), o, alts.get(q.id, ())).verdict for o in (a, b))
        assert (va in PASSING) == (vb in PASSING), q.id
        assert a.kind == b.kind, q.id
