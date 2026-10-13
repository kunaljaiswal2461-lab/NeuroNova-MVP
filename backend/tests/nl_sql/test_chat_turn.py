"""SPEC-09 L1-3/4/5: routing, the exact SSE order of every path, and what a SQL turn persists."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from agentic_engine.llm_engine.base_llm import LLMUnavailable
from app.schemas.nl_sql_record import NlSqlTurn
from app.services.nl_sql import messages
from app.services.nl_sql.prompt import PASS2_PROMPT_VERSION, PROMPT_VERSION
from app.services.sql_guard import RejectionCode
from tests.nl_sql.chat_harness import drive
from tests.nl_sql.conftest import GOOD_SQL, envelope
from tests.nl_sql.helpers import CLARIFY, UNSUPPORTED, ScriptedExecutor, failed_result, rejection, sql_env

NO_ENC = {"x": None, "y": [], "series": None, "label": None, "value": None}
KPI_SPEC = {
    "display": "kpi", "encodings": {**NO_ENC, "value": "n"}, "table_columns": [{"column": "n", "label": "Orders"}],
    "answer": "There are {{stat:n.sum}} orders.", "takeaways": ["Counted {{rowcount}} row"], "caveats": [],
}
GOOD = "SELECT count(*) AS n FROM sales.orders"
BAD = "SELECT cnt FROM sales.orders"
Q = "how many orders are there"


def settings_with(world, **changes):
    return world.settings.model_copy(update=changes)


# ── C1: the order of events, path by path ─────────────────────────────────────

async def test_sql_answer_order_and_payloads(synced, monkeypatch):
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC])
    assert run.types == [
        "start", "planning", "sql_generated", "executing", "postgres_result", "presentation", "token", "done",
    ]
    generated = run.data("sql_generated")[0]
    assert set(generated) == {"sql", "assumptions", "output_intent", "rewrites", "attempt"} and generated["attempt"] == 1
    assert run.data("executing")[0]["state"] == "executing" and run.data("executing")[0]["snapshot_id"]
    presentation = run.data("presentation")[0]
    assert presentation["answer"] == "There are 42 orders." and presentation["display"] == "kpi"
    assert set(presentation) == {"answer", "takeaways", "caveats", "display", "encodings", "table_columns", "fallback", "derived"}
    assert run.data("token")[0]["text"] == "There are 42 orders."
    assert run.data("postgres_result")[0]["postgres_result"]["rows"][0][0] == {"kind": "integer", "value": "42"}
    assert run.data("done")[0]["message_id"]


async def test_clarification_and_unsupported_order(synced, monkeypatch):
    for response, expected in ((CLARIFY, "clarification"), (UNSUPPORTED, "unsupported")):
        run = await drive(synced, monkeypatch, question=Q, responses=[response])
        assert run.types == ["start", "planning", expected, "token", "done"]
        assert run.executor.calls == []


async def test_repair_after_an_execution_failure(synced, monkeypatch):
    script = [failed_result("query_error", error_class="42703", sql=BAD), None]
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(BAD), sql_env(GOOD), KPI_SPEC], script=script)
    # fill the second scripted item with a real result
    assert run.types[:3] == ["start", "planning", "sql_generated"]


async def test_repair_events_in_order(synced, monkeypatch):
    from tests.nl_sql.helpers import ok_result
    from tests.nl_sql.chat_harness import head_snapshot

    snap = await head_snapshot(synced)
    script = [failed_result("query_error", error_class="42703", sql=BAD), ok_result(GOOD, 42, source_id=synced.source_id, snapshot_id=snap)]
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(BAD), sql_env(GOOD), KPI_SPEC], script=script)
    assert run.types == [
        "start", "planning", "sql_generated", "executing", "repairing", "sql_generated", "executing",
        "postgres_result", "presentation", "token", "done",
    ]
    assert run.data("repairing") == [{"reason_code": "query_error:42703"}]
    assert [d["attempt"] for d in run.data("sql_generated")] == [1, 2]
    record = run.assistant.nl_sql
    assert record.repair.reason_code == "query_error:42703" and record.repair.original_sql == BAD
    assert "The first query failed and was corrected automatically. Check the assumptions." in record.presentation.caveats


async def test_repair_after_a_rejection_has_no_executing_before_it(synced, monkeypatch):
    from tests.nl_sql.helpers import ok_result
    from tests.nl_sql.chat_harness import head_snapshot

    snap = await head_snapshot(synced)
    script = [rejection(RejectionCode.COLUMN_NOT_FOUND, "cnt"), ok_result(GOOD, 42, source_id=synced.source_id, snapshot_id=snap)]
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(BAD), sql_env(GOOD), KPI_SPEC], script=script)
    assert run.types == [
        "start", "planning", "sql_generated", "repairing", "sql_generated", "executing",
        "postgres_result", "presentation", "token", "done",
    ]


async def test_failed_turn_order_and_message(synced, monkeypatch):
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD)], script=[failed_result("execution_timeout")])
    assert run.types == ["start", "planning", "sql_generated", "executing", "error", "token", "done"]
    assert run.data("error")[0] == {"code": "execution_timeout", "message": messages.TIMEOUT}
    assert run.assistant.degraded and run.assistant.degraded_reason == "execution_timeout"
    assert run.assistant.content == messages.TIMEOUT
    assert run.assistant.nl_sql.outcome == "failed" and run.assistant.nl_sql.failure_code == "execution_timeout"


async def test_write_guard_order_and_no_model(synced, monkeypatch):
    run = await drive(synced, monkeypatch, question="delete all orders", responses=[sql_env(GOOD)])
    assert run.types == ["start", "planning", "unsupported", "token", "done"]
    assert run.llm.calls == [] and run.executor.calls == []                                        # I2
    assert run.assistant.content == messages.WRITE_REFUSAL and run.assistant.unsupported


async def test_cancelled_before_anything_follows_the_existing_path(synced, monkeypatch):
    from app.connectors.postgres_query_executor import PostgresExecutionCancellation

    cancellation = PostgresExecutionCancellation()
    await cancellation.cancel()
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD)], cancellation=cancellation)
    assert run.types[-2:] == ["token", "done"] and "cancelled" in run.data("token")[0]["text"]
    assert run.llm.calls == []


# ── routing (L1-3) ────────────────────────────────────────────────────────────

async def test_typed_only_never_enters_the_sql_path(synced, monkeypatch):
    seen = []

    async def plan(**kwargs):
        seen.append(kwargs["question"])
        from agentic_engine.conversational.postgres_planner import PostgresPlanningResult

        return PostgresPlanningResult(kind="unsupported", message="typed says no")

    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD)], plan=plan,
                      settings=settings_with(synced, nl_sql_route_mode="typed_only"))
    assert seen == [Q] and run.types[-3:] == ["unsupported", "token", "done"]
    assert run.executor.calls == [] and run.llm.calls == []


async def test_typed_only_does_not_even_apply_the_write_guard_here(synced, monkeypatch):
    seen = []

    async def plan(**kwargs):
        seen.append(kwargs["question"])
        from agentic_engine.conversational.postgres_planner import PostgresPlanningResult

        return PostgresPlanningResult(kind="unsupported", message="typed refusal")

    await drive(synced, monkeypatch, question="delete all orders", responses=[], plan=plan,
                settings=settings_with(synced, nl_sql_route_mode="typed_only"))
    assert seen == ["delete all orders"]                                                           # the old path decides


@pytest.mark.parametrize("kind", ["matched", "ambiguous"])
async def test_a_named_metric_keeps_the_typed_path(synced, monkeypatch, kind):
    seen = []

    async def plan(**kwargs):
        seen.append(kwargs["question"])
        from agentic_engine.conversational.postgres_planner import PostgresPlanningResult

        return PostgresPlanningResult(kind="clarification", message="which one?")

    run = await drive(synced, monkeypatch, question="net revenue", responses=[sql_env(GOOD)], plan=plan,
                      metric_match=SimpleNamespace(kind=kind))
    assert seen == ["net revenue"] and run.types[-3:] == ["clarification", "token", "done"]
    assert run.llm.calls == []


@pytest.mark.parametrize("match", [None, SimpleNamespace(kind="unavailable")])
async def test_no_metric_or_unavailable_goes_to_sql(synced, monkeypatch, match):
    async def plan(**kwargs):
        raise AssertionError("the typed planner must not run")

    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC], plan=plan, metric_match=match)
    assert "sql_generated" in run.types and run.assistant.nl_sql.route == "sql"


async def test_a_metric_half_match_falls_through_to_sql_exactly_once(synced, monkeypatch):
    from agentic_engine.conversational.postgres_planner import PostgresPlanningResult
    from agentic_engine.llm_engine.models import TokenUsage

    calls = []
    usage = TokenUsage()
    usage.add(30, 5)

    async def plan(**kwargs):
        calls.append(1)
        return PostgresPlanningResult(kind="unsupported", message="not an approved relationship", token_usage=usage)

    run = await drive(synced, monkeypatch, question="net revenue by region", responses=[sql_env(GOOD), KPI_SPEC],
                      plan=plan, metric_match=SimpleNamespace(kind="matched"))
    assert len(calls) == 1 and run.assistant.nl_sql.route == "sql_after_metric_fallthrough"
    assert run.types[:3] == ["start", "planning", "sql_generated"]
    assert run.assistant.token_usage.input_tokens == 100 + 100 + 30                                 # typed call + pass 1 + pass 2


async def test_a_typed_clarification_does_not_fall_through(synced, monkeypatch):
    from agentic_engine.conversational.postgres_planner import PostgresPlanningResult

    async def plan(**kwargs):
        return PostgresPlanningResult(kind="clarification", message="which year?")

    run = await drive(synced, monkeypatch, question="net revenue", responses=[sql_env(GOOD)], plan=plan,
                      metric_match=SimpleNamespace(kind="matched"))
    assert run.types[-3:] == ["clarification", "token", "done"] and run.llm.calls == []


async def test_no_api_key_gives_model_unavailable_on_the_sql_path_only(synced, monkeypatch):
    settings = settings_with(synced, openai_api_key="", nl_sql_stub_llm_path="")
    run = await drive(synced, monkeypatch, question=Q, responses=[], llm=None, settings=settings)
    assert run.types == ["start", "planning", "error", "token", "done"]
    assert run.data("error")[0] == {"code": "model_unavailable", "message": "The assistant model is not configured."}
    assert run.assistant.degraded_reason == "model_unavailable"


# ── persistence (L1-5) ────────────────────────────────────────────────────────

async def test_the_record_validates_for_every_terminal_path_and_never_leaks_sql_into_the_result(synced, monkeypatch):
    from tests.nl_sql.helpers import ok_result
    from tests.nl_sql.chat_harness import head_snapshot

    snap = await head_snapshot(synced)
    ok = ok_result(GOOD, 42, source_id=synced.source_id, snapshot_id=snap)
    cases = {
        "answer": ([sql_env(GOOD), KPI_SPEC], [ok]),
        "clarification": ([CLARIFY], None),
        "unsupported": ([UNSUPPORTED], None),
        "failed": ([sql_env(GOOD)], [failed_result("execution_timeout")]),
    }
    for outcome, (responses, script) in cases.items():
        run = await drive(synced, monkeypatch, question=Q, responses=responses, script=script)
        record = run.assistant.nl_sql
        assert isinstance(record, NlSqlTurn) and record.outcome == outcome and record.version == 1
        NlSqlTurn.model_validate(json.loads(record.model_dump_json()))                                # C2 round trip
        if outcome == "answer":
            stored_result = run.assistant.postgres_result.model_dump_json()
            assert "SELECT" not in stored_result.upper() and "count(*)" not in stored_result               # I14
            assert record.sql == GOOD
            assert record.prompt_versions == {"pass1": PROMPT_VERSION, "pass2": PASS2_PROMPT_VERSION}
            assert record.llm.calls == 2 and record.presentation.answer_template == "There are {{stat:n.sum}} orders."
            assert record.presentation.pass2.consent is False and record.presentation.pass2.rows_sent == 0


async def test_token_usage_is_the_sum_of_every_call(synced, monkeypatch):
    from tests.nl_sql.helpers import ok_result
    from tests.nl_sql.chat_harness import head_snapshot

    snap = await head_snapshot(synced)
    script = [rejection(RejectionCode.COLUMN_NOT_FOUND, "cnt"), ok_result(GOOD, 42, source_id=synced.source_id, snapshot_id=snap)]
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(BAD), sql_env(GOOD), KPI_SPEC], script=script)
    usage = run.assistant.token_usage
    assert (usage.input_tokens, usage.output_tokens) == (300, 60)
    assert run.assistant.nl_sql.llm.calls == 3 and run.assistant.nl_sql.llm.input_tokens == 300
    assert run.data("done")[0]["token_usage"]["input_tokens"] == 300


async def test_a_pass2_failure_is_never_a_turn_failure(synced, monkeypatch):
    for responses, reason in (
        ([sql_env(GOOD), LLMUnavailable()], "llm_unavailable"),
        ([sql_env(GOOD), {"display": "kpi"}], "spec_invalid"),
        ([sql_env(GOOD), {**KPI_SPEC, "answer": "Total is 7777."}], "number_check_failed"),
        ([sql_env(GOOD), {**KPI_SPEC, "answer": "{{row:0.zzz}}"}], "bad_reference"),
    ):
        run = await drive(synced, monkeypatch, question=Q, responses=responses)
        assert run.types[-4:] == ["postgres_result", "presentation", "token", "done"]
        presentation = run.assistant.nl_sql.presentation
        assert presentation.fallback == reason and presentation.display == "table"
        assert run.assistant.content == "The read-only calculation completed with 1 returned row(s)."
        assert not run.assistant.degraded and run.data("presentation")[0]["fallback"] == reason


async def test_the_pass2_system_prompt_is_static_and_the_user_message_has_no_cell_with_consent_off(synced, monkeypatch):
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC])
    pass2_call = run.llm.calls[1]
    assert pass2_call["schema_name"] == "pass2_spec" and pass2_call["temperature"] == 0.0
    assert "row values are not available" in pass2_call["user"]


# ── I8: the delivery guard around pass 2 ──────────────────────────────────────

async def test_a_revocation_before_pass_2_sends_no_row_anywhere(synced, monkeypatch):
    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC], revoke_after_first_guard=True)
    assert len(run.llm.calls) == 1                                                                  # pass 2 never ran
    assert run.types == ["start", "planning", "sql_generated", "executing", "error", "token", "done"]
    assert run.data("error")[0]["code"] == "access_revoked"
    assert run.assistant.degraded_reason == "access_revoked" and run.assistant.postgres_result is None
    assert "postgres_result" not in run.types


async def test_the_guard_is_checked_before_pass_2_and_held_for_persistence_and_emission(synced, monkeypatch):
    seen: list[tuple[int, int]] = []

    async def on_guard(run, n):
        seen.append((n, len(run.llm.calls)))

    run = await drive(synced, monkeypatch, question=Q, responses=[sql_env(GOOD), KPI_SPEC], on_guard=on_guard)
    assert run.types[-4:] == ["postgres_result", "presentation", "token", "done"]
    # (guard number, model calls made so far): the check runs after pass 1 and before pass 2; the other two
    # (persistence, emission) run after pass 2 has been made.
    assert seen == [(1, 1), (2, 2), (3, 2)]


# ── cancelling during the pass-2 model call ──────────────────────────────────

async def test_cancelling_during_pass_2_delivers_nothing(synced, monkeypatch):
    import asyncio

    from app.connectors.postgres_query_executor import PostgresExecutionCancellation

    cancellation = PostgresExecutionCancellation()
    in_pass2 = asyncio.Event()

    class HangsInPass2:
        def __init__(self):
            self.calls = []

        async def chat_structured(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["schema_name"] == "pass2_spec":
                in_pass2.set()
                await asyncio.sleep(30)
            from agentic_engine.llm_engine.base_llm import ChatResult

            return ChatResult(content_json=sql_env(GOOD), model="gpt-4o-test", input_tokens=100, output_tokens=20)

    async def cancel_when_pass2_starts():
        await in_pass2.wait()
        await cancellation.cancel()

    canceller = asyncio.create_task(cancel_when_pass2_starts())
    run = await asyncio.wait_for(
        drive(synced, monkeypatch, question=Q, responses=[], llm=HangsInPass2(), cancellation=cancellation), 10
    )
    await canceller
    assert "postgres_result" not in run.types and "presentation" not in run.types
    assert run.types[-2:] == ["token", "done"] and "cancelled" in run.data("token")[0]["text"]
    assert run.assistant.degraded_reason == "execution_cancelled"
