"""SPEC-10 W1.2 / W2.1 / W7: the turn budget, the `presenting` event, the revoked-path record, the empty-result caveat."""
from __future__ import annotations

import asyncio

import pytest

from agentic_engine.llm_engine.base_llm import ChatResult
from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.services.nl_sql import messages
from app.services.nl_sql.messages import FIXED_BY_CODE
from tests.nl_sql.chat_harness import drive
from tests.nl_sql.helpers import CLARIFY, UNSUPPORTED, failed_result, sql_env
from tests.nl_sql.test_chat_turn import BAD, GOOD, KPI_SPEC, Q, settings_with


class CountingCancellation(PostgresExecutionCancellation):
    def __init__(self):
        super().__init__()
        self.cancel_calls = 0

    async def cancel(self):
        self.cancel_calls += 1
        await super().cancel()


class SlowModel:
    """Pass 1 answers at once; the named schema sleeps ``delay`` seconds first."""

    def __init__(self, slow_schema: str, delay: float, responses: dict):
        self.slow_schema, self.delay, self.responses = slow_schema, delay, responses
        self.calls: list[str] = []
        self.completed: list[str] = []

    async def chat_structured(self, **kwargs):
        name = kwargs["schema_name"]
        self.calls.append(name)
        if name == self.slow_schema:
            await asyncio.sleep(self.delay)
        self.completed.append(name)
        return ChatResult(content_json=self.responses[name], model="gpt-4o-test", input_tokens=100, output_tokens=20)


# ── W2.1: presenting ─────────────────────────────────────────────────────────

async def test_presenting_is_absent_on_every_path_that_never_reaches_pass_2(synced, monkeypatch):
    clarification = await drive(synced, monkeypatch, question=Q, responses=[CLARIFY])
    assert "presenting" not in clarification.types
    unsupported = await drive(synced, monkeypatch, question=Q, responses=[UNSUPPORTED])
    assert "presenting" not in unsupported.types
    failed = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD)], script=[failed_result("execution_timeout")])
    assert "presenting" not in failed.types
    guard = await drive(synced, monkeypatch, question="delete all orders", responses=[sql_env(GOOD)])
    assert "presenting" not in guard.types
    revoked = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC], revoke_after_first_guard=True)
    assert "presenting" not in revoked.types                                      # revoked before pass 2


async def test_presenting_comes_after_the_last_executing_and_before_the_result(synced, monkeypatch):
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC])
    assert run.data("presenting") == [{"state": "presenting"}]
    assert run.types.index("presenting") > max(i for i, t in enumerate(run.types) if t == "executing")
    assert run.types.index("presenting") < run.types.index("postgres_result")


async def test_presenting_never_appears_on_the_typed_path(synced, monkeypatch):
    run = await drive(synced, monkeypatch, question=Q, responses=[], settings=settings_with(synced, nl_sql_route_mode="typed_only"),
                      plan=_clarifying_plan)
    assert "presenting" not in run.types and "sql_generated" not in run.types


async def _clarifying_plan(**_kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(executable=False, kind="clarification", message="Which table?", snapshot_id=None,
                           token_usage=None, metric_definition=None)


# ── W1.2: the turn budget ────────────────────────────────────────────────────

async def test_the_budget_expiring_during_pass_2_is_a_turn_timeout_not_a_cancellation(synced, monkeypatch):
    """E3."""
    cancellation = CountingCancellation()
    model = SlowModel("pass2_spec", 5.0, {"pass1_envelope": sql_env(GOOD), "pass2_spec": KPI_SPEC})
    run = await asyncio.wait_for(drive(
        synced, monkeypatch, question=Q, responses=[], llm=model, cancellation=cancellation,
        settings=settings_with(synced, nl_sql_turn_budget_seconds=0.3),
    ), 10)
    assert run.types[-3:] == ["error", "token", "done"] and "postgres_result" not in run.types
    assert run.data("error")[0] == {"code": "turn_timeout", "message": messages.TURN_TIMEOUT}
    assert cancellation.cancel_calls == 1
    assistant = run.assistant
    assert assistant.degraded and assistant.degraded_reason == "turn_timeout" and assistant.content == messages.TURN_TIMEOUT
    assert assistant.nl_sql.outcome == "failed" and assistant.nl_sql.failure_code == "turn_timeout"
    assert assistant.nl_sql.sql == GOOD and assistant.postgres_result is None
    # Greptile: pass 1 finished before pass 2 timed out, so its call and tokens are kept; only pass 2's are unknown.
    assert assistant.nl_sql.llm.calls == 1 and assistant.nl_sql.llm.input_tokens == 100
    assert assistant.token_usage is not None and assistant.token_usage.input_tokens == 100
    assert "cancelled" not in run.data("token")[0]["text"]


async def test_the_budget_expiring_during_pass_1_keeps_the_last_attempted_sql_out_of_a_cancellation(synced, monkeypatch):
    cancellation = CountingCancellation()
    model = SlowModel("pass1_envelope", 5.0, {"pass1_envelope": sql_env(GOOD), "pass2_spec": KPI_SPEC})
    run = await asyncio.wait_for(drive(
        synced, monkeypatch, question=Q, responses=[], llm=model, cancellation=cancellation,
        settings=settings_with(synced, nl_sql_turn_budget_seconds=0.3),
    ), 10)
    assert run.data("error")[0]["code"] == "turn_timeout" and cancellation.cancel_calls == 1
    assert run.assistant.nl_sql.sql is None and run.assistant.degraded_reason == "turn_timeout"


async def test_a_budget_expiring_after_persistence_started_does_not_interrupt_the_answer(synced, monkeypatch):
    """E4."""
    async def slow_guard(_run, call_number):
        if call_number >= 2:                       # after pass 2: persistence and emission
            await asyncio.sleep(2.2)

    run = await drive(
        synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC], on_guard=slow_guard,
        settings=settings_with(synced, nl_sql_turn_budget_seconds=2.0),
    )
    assert run.types[-4:] == ["postgres_result", "presentation", "token", "done"] and "error" not in run.types


def test_turn_timeout_is_a_fixed_message():
    assert FIXED_BY_CODE["turn_timeout"] == messages.TURN_TIMEOUT
    assert messages.INTERNAL == "Something went wrong while answering. Please try again."


# ── W7.1: the revoked path persists its record ───────────────────────────────

async def test_a_revoked_turn_persists_its_record(synced, monkeypatch):
    """E41."""
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC], revoke_after_first_guard=True)
    assert run.assistant.degraded_reason == "access_revoked"
    record = run.assistant.nl_sql
    assert record is not None and record.sql == GOOD
    # Greptile: the saved record must say "failed", or a reload shows an answer card and loses the SQL panel.
    assert record.outcome == "failed" and record.failure_code == "access_revoked"


# ── W7.3: an empty result is not a "does not fit" case ───────────────────────

async def test_an_empty_result_downgrades_the_chart_without_the_does_not_fit_caveat(synced, monkeypatch):
    """E24."""
    from tests.nl_sql.helpers import ok_result
    from tests.nl_sql.pass2_helpers import I
    from tests.nl_sql.chat_harness import head_snapshot

    snapshot = await head_snapshot(synced)
    empty = ok_result(snapshot_id=snapshot, source_id=synced.source_id)
    empty = empty.model_copy(update={"rows": (), "row_count": 0})
    bar = {**KPI_SPEC, "display": "bar", "encodings": {"x": "n", "y": ["n"], "series": None, "label": None, "value": None},
           "answer": "No rows."}
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), bar], script=[empty])
    presentation = run.data("presentation")[0]
    assert presentation["display"] == "table"
    assert presentation["fallback"] and presentation["fallback"].startswith("display_downgraded:")
    assert not any("does not fit" in caveat for caveat in presentation["caveats"])
