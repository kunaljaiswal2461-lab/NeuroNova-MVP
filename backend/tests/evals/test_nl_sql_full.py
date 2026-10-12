"""SPEC-09 W7 (built here, run for real by the tester): the full-pipeline candidate's pure parts and the new report."""
from __future__ import annotations

import pytest

from app.services.nl_sql.history import history_from_messages
from app.services.nl_sql.pipeline import AttemptRecord
from app.services.sql_execution import SqlExecutionResult, SqlResultColumn
from app.services.postgres_query_executor import PostgresResultValue
from evals import report as rpt
from evals.candidates.base import CandidateOutcome, LLMCall
from evals.candidates.nl_sql_full import NlSqlFullCandidate, _from_attempt, chain_history
from evals.grader import Verdict
from evals.models import GoldenQuestion, load_questions
import uuid


# ── chains ────────────────────────────────────────────────────────────────────

def test_the_chain_suite_has_twenty_chains_in_the_required_mix():
    chains = load_questions("chains")
    assert len(chains) == 20 and len({c.id for c in chains}) == 20
    mix = {tag: sum(tag in c.tags for c in chains) for tag in (
        "refinement", "regrouping", "period_shift", "topic_switch", "clarification_followup", "failed_followup")}
    assert mix == {"refinement": 6, "regrouping": 4, "period_shift": 3, "topic_switch": 3, "clarification_followup": 2, "failed_followup": 2}
    for chain in chains:
        assert "chain" in chain.tags and 1 <= len(chain.turns) <= 3                # 2-4 turns in all
        assert chain.expect.kind == "answer" and chain.expect.reference_sql and chain.reviewed_by is None
        for turn in chain.turns:
            assert "question" in turn and sum(k in turn for k in ("reference_sql", "clarification", "failed")) == 1


def test_golden_and_adversarial_suites_are_unchanged_by_the_chains():
    assert len(load_questions("golden")) == 150 and len(load_questions("adversarial")) == 10
    assert not any("chain" in q.tags for q in load_questions("all"))


def test_chain_history_goes_through_the_chat_history_builder():
    turns = ({"question": "q1", "reference_sql": "SELECT 1\n"},
             {"question": "q2", "clarification": "Which year?"},
             {"question": "q3", "failed": True})
    history = chain_history(turns)
    assert [(t.question, t.sql, t.outcome) for t in history] == [
        ("q1", "SELECT 1", "answer"), ("q2", None, "clarification"), ("q3", None, "failed")]
    assert history[1].assumptions == ("asked: Which year?",)
    assert chain_history(None) == [] and chain_history(()) == []


def test_only_the_last_three_turns_of_a_long_chain_are_used():
    turns = tuple({"question": f"q{i}", "reference_sql": f"SELECT {i}"} for i in range(5))
    assert [t.question for t in chain_history(turns)] == ["q2", "q3", "q4"]


def test_every_chain_reference_parses_and_has_no_clock_call():
    from evals.lint import clock_calls

    for chain in load_questions("chains"):
        assert not clock_calls(chain.expect.reference_sql)
        for turn in chain.turns:
            if "reference_sql" in turn:
                assert not clock_calls(turn["reference_sql"])


# ── first attempt view ────────────────────────────────────────────────────────

def _execution():
    return SqlExecutionResult(
        run_id=uuid.uuid4(), status="succeeded", sql="SELECT 1", columns=(SqlResultColumn(label="n", pg_type="int8"),),
        rows=((PostgresResultValue(kind="integer", value="1"),),), row_count=1, source_id=uuid.uuid4(),
    )


def test_the_first_attempt_becomes_a_candidate_outcome():
    ok = _from_attempt(AttemptRecord("SELECT 1", "answer", None, _execution()))
    assert ok.kind == "answer" and ok.result.rows[0][0] == 1
    bad = _from_attempt(AttemptRecord("SELECT x", "rejected", "column_not_found"))
    assert (bad.kind, bad.error_code, bad.sql) == ("error", "column_not_found", "SELECT x")
    assert _from_attempt(AttemptRecord(None, "pass1_failed", "envelope_invalid")).error_code == "envelope_invalid"
    assert _from_attempt(AttemptRecord(None, "clarification")).kind == "clarification"
    assert _from_attempt(AttemptRecord(None, "unsupported", "write_request")).kind == "refusal"


def test_the_candidate_is_registered_and_declares_what_it_needs():
    from evals.runner import load_candidate

    candidate = load_candidate("nl_sql_full")
    assert isinstance(candidate, NlSqlFullCandidate)
    assert (candidate.name, candidate.requires_app, candidate.uses_model) == ("nl_sql_full", True, True)


# ── the report sections ───────────────────────────────────────────────────────

def record(qid, verdict=Verdict.PASS, **fields):
    base = dict(question_id=qid, tags=[], expected_kind="answer", run_index=0, verdict=verdict, output_type="bar",
                llm_calls=2, input_tokens=100, output_tokens=20, models=["gpt-4o"])
    base.update(fields)
    return rpt.RunRecord(**base)


PRICING = {"gpt-4o": {"input_per_mtok": 2.5, "output_per_mtok": 10.0}}


def test_repair_numbers_come_from_the_first_attempt_and_the_final_verdict():
    wrong = Verdict.FAIL_VALUES
    records = [
        record("a", Verdict.PASS, first_verdict=Verdict.PASS),
        record("b", Verdict.PASS, first_verdict=wrong, repair_reason="column_not_found"),
        record("c", wrong, first_verdict=wrong, repair_reason="query_error:42803"),
        record("d", wrong, first_verdict=wrong, repair_reason="column_not_found"),
    ]
    stats = rpt.aggregate(records, PRICING).pipeline
    assert stats.accuracy_without_repair.p == pytest.approx(0.25) and rpt.accuracy_of(records).p == pytest.approx(0.5)
    assert stats.repair_rate == pytest.approx(0.75) and stats.repair_success_rate == pytest.approx(1 / 3)
    assert {k: (v.attempted, v.succeeded) for k, v in stats.repair_by_reason.items()} == {
        "column_not_found": (2, 1), "query_error:42803": (1, 0)}


def test_pass2_sections():
    records = [
        record("a", pass2_ran=True, pass2_display="bar"),
        record("b", pass2_ran=True, pass2_display="table", pass2_fallback="number_check_failed", pass2_dropped=2),
        record("c", pass2_ran=True, pass2_display="bar", pass2_fallback="display_downgraded:pie->bar", pass2_dropped=1),
        record("d", pass2_ran=True, pass2_display="table", pass2_fallback="llm_unavailable"),
    ]
    stats = rpt.aggregate(records, PRICING).pipeline
    assert stats.pass2_runs == 4 and stats.display_accuracy.p == pytest.approx(0.5)
    assert stats.pass2_fallbacks == {"llm_unavailable": 1, "number_check_failed": 1}       # a downgrade is not a fallback
    assert stats.pass2_fallback_rate == pytest.approx(0.5)
    assert stats.number_check_drop_rate == pytest.approx(0.5)


def test_chain_vs_single_turn_and_cost_per_answer():
    records = [record("s1"), record("s2", Verdict.FAIL_VALUES),
               record("c1", chain=True), record("c2", chain=True)]
    stats = rpt.aggregate(records, PRICING).pipeline
    assert stats.chain_accuracy.p == 1.0 and stats.single_turn_accuracy.p == 0.5
    assert stats.calls_per_answer == 2 and stats.input_tokens_per_answer == 100 and stats.output_tokens_per_answer == 20
    assert stats.cost_per_answer_usd == pytest.approx((100 / 1e6 * 2.5) + (20 / 1e6 * 10.0))


def test_personal_value_leaks_are_counted_and_shown():
    records = [record("a", pass2_ran=True, notes=["pass2_personal_value_in_prompt"]), record("b", pass2_ran=True)]
    report = rpt.Report(
        meta=rpt.RunMeta(git_commit=None, fixture_version=1, candidate="nl_sql_full", suite="golden", models=[], repeat=1,
                         started_at="2026-10-12T00:00:00Z", finished_at="2026-10-12T00:00:01Z"),
        aggregates=rpt.aggregate(records, PRICING), records=records,
    )
    assert report.aggregates.pipeline.personal_value_leaks == 1
    text = rpt.render_markdown(report)
    assert "Personal values found in pass-2 prompts (consent on):** 1" in text
    assert "Pipeline (SPEC-09)" in text and "Pass-2 fallback rate" in text


def test_records_without_pipeline_fields_get_no_pipeline_section():
    records = [record("a", llm_calls=0)]
    assert rpt.aggregate(records, PRICING).pipeline is None
    assert rpt.RunRecord.model_validate(records[0].model_dump(mode="json", exclude={"repair_reason", "pass2_ran", "chain"})).chain is False


def test_a_report_written_before_part_9_still_loads():
    old = {"question_id": "x", "tags": [], "expected_kind": "answer", "run_index": 0, "verdict": "pass"}
    assert rpt.RunRecord.model_validate(old).pass2_ran is False


def test_the_runner_records_the_new_fields_and_grades_the_first_attempt():
    from evals.runner import _record
    from evals.grader import Grade

    q = GoldenQuestion.model_validate({"id": "q", "question": "?", "tags": ["chain"], "turns": [{"question": "a", "failed": True}],
                                       "expect": {"kind": "answer", "reference_sql": "SELECT 1", "output_type": "bar"}})
    outcome = CandidateOutcome("answer", llm_calls=(LLMCall("gpt-4o", 5, 1, 1.0), LLMCall("gpt-4o", 5, 1, 1.0)),
                               presentation={"display": "bar"}, pass2_ran=True, pass2_dropped=1, repair_reason="x")
    rec = _record(q, 0, outcome, Grade(Verdict.PASS, ""), Verdict.PASS)
    assert (rec.chain, rec.pass2_display, rec.pass2_dropped, rec.repair_reason, rec.llm_calls) == (True, "bar", 1, "x", 2)
    assert rec.first_verdict is Verdict.PASS


# ── the strict-schema smoke test (offline half) ───────────────────────────────

def test_both_schemas_satisfy_the_strict_mode_rules_we_can_check_offline():
    from app.services.nl_sql import ENVELOPE_JSON_SCHEMA
    from app.services.nl_sql.pass2_spec import PASS2_JSON_SCHEMA
    from evals.smoke_structured import check_strict

    assert check_strict(ENVELOPE_JSON_SCHEMA) == []
    assert check_strict(PASS2_JSON_SCHEMA) == []


def test_check_strict_finds_what_it_is_for():
    from evals.smoke_structured import check_strict

    bad = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 5}, "b": {"type": "object", "properties": {"c": {"type": "string"}}}}, "required": ["a"]}
    problems = check_strict(bad)
    assert any("additionalProperties" in p for p in problems)
    assert any("every property must be required" in p for p in problems)
    assert any("maxLength" in p for p in problems)
    assert any("$.b" in p for p in problems)
