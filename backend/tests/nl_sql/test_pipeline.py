"""SPEC-09 L1-1: the repair state machine, one test per sequence, asserting call count, executions and messages."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agentic_engine.llm_engine.base_llm import ChatResult, LLMUnavailable
from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.services.nl_sql import answer_question, messages
from app.services.nl_sql.repair import SQLSTATE_PROBLEMS
from app.services.sql_guard import RejectionCode
from tests.nl_sql.conftest import GOOD_SQL, REF_TIME, ZONE, StubLLM, envelope
from tests.nl_sql.helpers import CLARIFY, UNSUPPORTED, ScriptedExecutor, failed_result, ok_result, rejection, sql_env

Q = "how many orders are there"
BAD = "SELECT cnt FROM sales.orders"
GOOD = "SELECT count(*) AS n FROM sales.orders"


async def run(world, llm, executor, *, events=None, **kwargs):
    async def on_event(event):
        if events is not None:
            events.append((event.name, event.data))

    return await answer_question(
        session_factory=world.factory, settings=world.settings, llm=llm, user_id=world.owner_id,
        source_id=world.source_id, question=Q, reference_time=REF_TIME, time_zone=ZONE, execute=executor,
        on_event=on_event, **kwargs,
    )


def brief_of(llm: StubLLM, index: int = 1) -> str:
    return llm.calls[index]["user"]


# ── the good path ─────────────────────────────────────────────────────────────

async def test_good_answer_needs_one_call_and_one_execution(synced):
    llm, ex, events = StubLLM([sql_env(GOOD)]), ScriptedExecutor([ok_result()]), []
    out = await run(synced, llm, ex, events=events)
    assert out.kind == "answer" and out.repair is None and out.failure_code is None
    assert len(llm.calls) == 1 and len(ex.calls) == 1
    assert [e[0] for e in events] == ["sql_generated", "executing"]
    assert events[0][1]["attempt"] == 1 and events[0][1]["sql"] == GOOD
    assert out.sql == out.execution.sql and out.snapshot_id is not None
    assert len(out.llm_calls) == 1 and out.attempts[0].outcome == "answer"


# ── bad -> good, for every repairable class ───────────────────────────────────

REPAIRABLE_REJECTIONS = [
    RejectionCode.PARSE_ERROR, RejectionCode.UNSUPPORTED_SYNTAX, RejectionCode.MULTIPLE_STATEMENTS,
    RejectionCode.UNSUPPORTED_STATEMENT, RejectionCode.TOO_COMPLEX, RejectionCode.NO_TABLE_REFERENCE,
    RejectionCode.TABLE_NOT_IN_SCOPE, RejectionCode.AMBIGUOUS_TABLE, RejectionCode.DATABASE_QUALIFIED_TABLE,
    RejectionCode.COLUMN_NOT_FOUND, RejectionCode.AMBIGUOUS_COLUMN, RejectionCode.FUNCTION_NOT_ALLOWED,
    RejectionCode.SCHEMA_QUALIFIED_FUNCTION, RejectionCode.CAST_TYPE_NOT_ALLOWED,
    RejectionCode.TABLE_FUNCTION_NOT_ALLOWED, RejectionCode.NON_LITERAL_LIMIT,
]


@pytest.mark.parametrize("code", REPAIRABLE_REJECTIONS)
async def test_a_validator_rejection_is_repaired_once(synced, code):
    llm = StubLLM([sql_env(BAD), sql_env(GOOD)])
    ex, events = ScriptedExecutor([rejection(code, "cnt", ("n",)), ok_result()]), []
    out = await run(synced, llm, ex, events=events)
    assert out.kind == "answer" and out.repair is not None
    assert out.repair.reason_code == code.value and out.repair.original_sql == BAD and out.repair.attempts == 1
    assert len(llm.calls) == 2 and len(ex.calls) == 2                                   # I1
    brief = brief_of(llm)
    assert "Your previous answer could not be used." in brief
    assert f"problem: {code.value}" in brief and f"Fixed validator message for {code.value}." in brief
    assert 'previous_sql: "SELECT cnt FROM sales.orders"' in brief
    assert 'identifier: "cnt"' in brief and 'did_you_mean: ["n"]' in brief
    assert brief.rstrip().endswith("Write a corrected answer for the same question.")
    # C1: a rejection never reaches "executing" before the repair.
    assert [e[0] for e in events] == ["sql_generated", "repairing", "sql_generated", "executing"]
    assert events[1][1] == {"reason_code": code.value} and events[2][1]["attempt"] == 2


@pytest.mark.parametrize("sqlstate", sorted(SQLSTATE_PROBLEMS))
async def test_a_query_error_is_repaired_with_its_fixed_sentence(synced, sqlstate):
    llm = StubLLM([sql_env(BAD), sql_env(GOOD)])
    server_text = 'column "cnt" does not exist DETAIL: secret-row-value HINT: Perhaps you meant n.'
    ex, events = ScriptedExecutor([failed_result("query_error", error_class=sqlstate, message=server_text), ok_result()]), []
    out = await run(synced, llm, ex, events=events)
    assert out.kind == "answer" and out.repair.reason_code == f"query_error:{sqlstate}"
    assert len(llm.calls) == 2 and len(ex.calls) == 2
    brief = brief_of(llm)
    assert f"problem: query_error — {SQLSTATE_PROBLEMS[sqlstate]}" in brief
    for forbidden in ("DETAIL", "HINT", "secret-row-value", "does not exist DETAIL", sqlstate):    # I11
        assert forbidden not in brief
    assert [e[0] for e in events] == ["sql_generated", "executing", "repairing", "sql_generated", "executing"]


@pytest.mark.parametrize("code,expected", [
    ("query_too_expensive", "The query is too expensive. Filter earlier, avoid cross joins, aggregate before joining large tables."),
    ("result_row_too_large", "A result row is too large. Select fewer or shorter columns."),
    ("unsupported_output_type", "Output column 2 has a type that cannot be returned: cast to numeric."),
])
async def test_execution_gates_are_repairable(synced, code, expected):
    llm = StubLLM([sql_env(BAD), sql_env(GOOD)])
    ex = ScriptedExecutor([failed_result(code, message=expected), ok_result()])
    out = await run(synced, llm, ex)
    assert out.kind == "answer" and out.repair.reason_code == code
    assert f"problem: {code} — {expected}" in brief_of(llm)


async def test_envelope_invalid_is_repaired_without_running_anything_first(synced):
    llm = StubLLM([{"kind": "sql"}, sql_env(GOOD)])
    ex, events = ScriptedExecutor([ok_result()]), []
    out = await run(synced, llm, ex, events=events)
    assert out.kind == "answer" and out.repair.reason_code == "envelope_invalid:not_valid" and out.repair.original_sql == ""
    assert len(llm.calls) == 2 and len(ex.calls) == 1
    brief = brief_of(llm)
    assert "Your previous output was not valid for the required format (not_valid)." in brief
    assert "previous_sql" not in brief
    assert [e[0] for e in events] == ["repairing", "sql_generated", "executing"]


@pytest.mark.parametrize("finish,sub", [("length", "length"), ("refusal", "refusal"), ("invalid_json", "invalid_json")])
async def test_envelope_sub_codes(synced, finish, sub):
    cut = ChatResult({}, "gpt-4o-test", 10, 0, finish_reason=finish)
    llm = StubLLM([cut, sql_env(GOOD)])
    out = await run(synced, llm, ScriptedExecutor([ok_result()]))
    assert out.kind == "answer" and f"({sub})" in brief_of(llm)


# ── bad -> bad / same / other kinds ───────────────────────────────────────────

async def test_bad_then_bad_fails_with_the_message_of_the_second_error(synced):
    llm = StubLLM([sql_env(BAD), sql_env("SELECT other FROM sales.orders")])
    ex = ScriptedExecutor([
        rejection(RejectionCode.AMBIGUOUS_COLUMN, "id"), failed_result("query_too_expensive"),
    ])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and out.failure_code == "query_too_expensive"
    assert out.user_message == messages.TOO_EXPENSIVE
    assert len(llm.calls) == 2 and len(ex.calls) == 2                                   # I1: never a third


async def test_bad_then_bad_column_message_uses_catalog_names_only(synced):
    llm = StubLLM([sql_env(BAD), sql_env("SELECT cnt2 FROM sales.orders")])
    ex = ScriptedExecutor([
        rejection(RejectionCode.COLUMN_NOT_FOUND, "cnt"),
        rejection(RejectionCode.COLUMN_NOT_FOUND, "cnt2", ("order_id", "ignored; DROP", "status")),
    ])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and out.failure_code == "column_not_found"
    assert "cnt2" not in out.user_message                       # the model's identifier is not a catalog name
    assert "order_id" in out.user_message and "status" in out.user_message
    assert "ignored; DROP" not in out.user_message              # a suggestion that is not in the catalog is dropped
    assert out.user_message.startswith("I couldn't find a matching column.")


async def test_a_real_catalog_column_name_is_echoed(synced):
    llm = StubLLM([sql_env(BAD), sql_env(BAD + " x")])
    ex = ScriptedExecutor([rejection(RejectionCode.COLUMN_NOT_FOUND, "note"), rejection(RejectionCode.COLUMN_NOT_FOUND, "note")])
    out = await run(synced, llm, ex)
    assert "matching 'note'" in out.user_message


async def test_table_not_in_scope_lists_catalog_tables(synced):
    llm = StubLLM([sql_env(BAD), sql_env(BAD + " ")])
    ex = ScriptedExecutor([rejection(RejectionCode.TABLE_NOT_IN_SCOPE, "ghost", ("sales.orders", "nope.x")),
                           rejection(RejectionCode.TABLE_NOT_IN_SCOPE, "ghost", ("sales.orders", "nope.x"))])
    out = await run(synced, llm, ex)
    assert out.user_message == "That data isn't in the tables this source can read: sales.orders."


async def test_bad_then_the_same_sql_is_not_executed_twice(synced):
    llm = StubLLM([sql_env(BAD), sql_env("SELECT   cnt\nFROM sales.orders")])
    ex = ScriptedExecutor([failed_result("query_error", error_class="42703")])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and out.user_message == messages.REPHRASE and out.failure_code == "query_error"
    assert len(ex.calls) == 1 and len(llm.calls) == 2                                   # I3


async def test_same_sql_with_other_casing_or_layout_counts_as_the_same(synced):
    llm = StubLLM([sql_env("SELECT Cnt FROM sales.orders"), sql_env("select cnt\n  from sales.orders")])
    ex = ScriptedExecutor([rejection(RejectionCode.COLUMN_NOT_FOUND, "cnt")])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and len(ex.calls) == 1


async def test_bad_then_clarification_is_accepted(synced):
    out = await run(synced, StubLLM([sql_env(BAD), CLARIFY]), ScriptedExecutor([rejection(RejectionCode.PARSE_ERROR)]))
    assert out.kind == "clarification" and out.user_message == "Which year do you mean?" and out.repair is None


async def test_bad_then_unsupported_is_accepted(synced):
    out = await run(synced, StubLLM([sql_env(BAD), UNSUPPORTED]), ScriptedExecutor([rejection(RejectionCode.PARSE_ERROR)]))
    assert out.kind == "unsupported" and out.failure_code == "not_in_schema" and out.repair is None
    assert out.user_message == "That information isn't available in the tables this source can read."


async def test_repair_that_fails_in_the_model_fails_the_turn(synced):
    llm = StubLLM([sql_env(BAD), LLMUnavailable()])
    out = await run(synced, llm, ScriptedExecutor([rejection(RejectionCode.PARSE_ERROR)]))
    assert out.kind == "failed" and out.failure_code == "llm_unavailable" and out.user_message == messages.MODEL_UNAVAILABLE


# ── schema_changed: regenerate once ───────────────────────────────────────────

async def test_schema_changed_regenerates_without_a_brief(synced):
    llm = StubLLM([sql_env(GOOD), sql_env(GOOD)])
    ex, events = ScriptedExecutor([failed_result("schema_changed"), ok_result()]), []
    out = await run(synced, llm, ex, events=events)
    assert out.kind == "answer" and out.repair.reason_code == "schema_changed"
    assert len(llm.calls) == 2 and len(ex.calls) == 2
    assert llm.calls[0]["user"] == llm.calls[1]["user"]                                  # same question, no brief
    assert [e[0] for e in events][:3] == ["sql_generated", "executing", "repairing"]


async def test_schema_changed_twice_is_final(synced):
    llm = StubLLM([sql_env(GOOD)])
    ex = ScriptedExecutor([failed_result("schema_changed")])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and out.failure_code == "schema_changed" and out.user_message == messages.GENERIC
    assert len(llm.calls) == 2 and len(ex.calls) == 2                                    # one regenerate, never more


# ── final classes: no repair call ─────────────────────────────────────────────

FINAL_EXECUTION = [
    ("execution_timeout", messages.TIMEOUT), ("access_changed", messages.ACCESS_CHANGED),
    ("access_revoked", messages.ACCESS_REVOKED), ("restricted_role_required", messages.GENERIC),
    ("source_unavailable", messages.GENERIC), ("tls_verification_failed", messages.GENERIC),
    ("credential_unavailable", messages.GENERIC), ("catalog_unavailable", messages.GENERIC),
    ("execution_recording_failed", messages.GENERIC), ("execution_failed", messages.GENERIC),
]


@pytest.mark.parametrize("code,text", FINAL_EXECUTION)
async def test_final_execution_failures_make_no_repair_call(synced, code, text):
    llm, ex = StubLLM([sql_env(GOOD)]), ScriptedExecutor([failed_result(code)])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and out.failure_code == code and out.user_message == text
    assert len(llm.calls) == 1 and len(ex.calls) == 1


@pytest.mark.parametrize("code", [
    RejectionCode.EMPTY_INPUT, RejectionCode.INPUT_TOO_LARGE, RejectionCode.INVALID_TEXT, RejectionCode.INTERNAL_ERROR,
])
async def test_final_validator_rejections_make_no_repair_call(synced, code):
    llm, ex = StubLLM([sql_env(GOOD)]), ScriptedExecutor([rejection(code)])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and out.failure_code == code.value and out.user_message == messages.GENERIC
    assert len(llm.calls) == 1 and len(ex.calls) == 1


async def test_a_query_error_with_an_unlisted_sqlstate_is_final(synced):
    llm = StubLLM([sql_env(GOOD)])
    out = await run(synced, llm, ScriptedExecutor([failed_result("query_error", error_class="23505")]))
    assert out.kind == "failed" and len(llm.calls) == 1 and out.user_message == messages.REPHRASE


@pytest.mark.parametrize("response,code,text", [
    (LLMUnavailable(), "llm_unavailable", messages.MODEL_UNAVAILABLE),
    (envelope(assumptions=["NN-PASS1-CANARY-7c3e91ab"]), "canary_leak", messages.UNSAFE),
])
async def test_final_pass1_failures_make_no_repair_call(synced, response, code, text):
    llm, ex = StubLLM([response]), ScriptedExecutor([ok_result()])
    out = await run(synced, llm, ex)
    assert out.kind == "failed" and out.failure_code == code and out.user_message == text
    assert len(llm.calls) == 1 and ex.calls == []


async def test_question_too_long_and_empty_never_call_the_model(synced):
    llm = StubLLM()
    out = await answer_question(
        session_factory=synced.factory, settings=synced.settings, llm=llm, user_id=synced.owner_id,
        source_id=synced.source_id, question="x" * 5_000, reference_time=REF_TIME, time_zone=ZONE,
        execute=ScriptedExecutor([ok_result()]),
    )
    assert out.failure_code == "question_too_long" and out.user_message == messages.QUESTION_TOO_LONG and llm.calls == []


async def test_a_write_request_never_reaches_the_model_or_the_executor(synced):
    llm, ex = StubLLM(), ScriptedExecutor([ok_result()])
    out = await answer_question(
        session_factory=synced.factory, settings=synced.settings, llm=llm, user_id=synced.owner_id,
        source_id=synced.source_id, question="delete all orders", reference_time=REF_TIME, time_zone=ZONE, execute=ex,
    )
    assert out.kind == "unsupported" and out.user_message == messages.WRITE_REFUSAL
    assert llm.calls == [] and ex.calls == []                                            # I2


async def test_clarification_and_unsupported_are_final_without_repair(synced):
    for response, kind in ((CLARIFY, "clarification"), (UNSUPPORTED, "unsupported")):
        llm, ex = StubLLM([response]), ScriptedExecutor([ok_result()])
        out = await run(synced, llm, ex)
        assert out.kind == kind and len(llm.calls) == 1 and ex.calls == []


# ── attempts, budget, cancellation ────────────────────────────────────────────

async def test_the_first_attempt_is_kept_for_the_eval_report(synced):
    llm = StubLLM([sql_env(BAD), sql_env(GOOD)])
    out = await run(synced, llm, ScriptedExecutor([rejection(RejectionCode.COLUMN_NOT_FOUND, "cnt"), ok_result()]))
    assert [a.outcome for a in out.attempts] == ["rejected", "answer"]
    assert out.attempts[0].code == "column_not_found" and out.attempts[0].sql == BAD


async def test_token_calls_are_collected_for_every_model_call(synced):
    llm = StubLLM([sql_env(BAD), sql_env(GOOD)])
    out = await run(synced, llm, ScriptedExecutor([rejection(RejectionCode.COLUMN_NOT_FOUND, "cnt"), ok_result()]))
    assert [(c.input_tokens, c.output_tokens) for c in out.llm_calls] == [(100, 20), (100, 20)]


async def test_a_cancelled_turn_calls_nothing(synced):
    cancellation = PostgresExecutionCancellation()
    await cancellation.cancel()
    llm, ex = StubLLM(), ScriptedExecutor([ok_result()])
    out = await run(synced, llm, ex, cancellation=cancellation)
    assert out.kind == "failed" and out.failure_code == "execution_cancelled" and out.user_message == messages.CANCELLED
    assert llm.calls == [] and ex.calls == []


async def test_cancelling_during_the_model_call_drops_the_call(synced):
    import asyncio

    cancellation = PostgresExecutionCancellation()
    started = asyncio.Event()

    class SlowLLM:
        calls = []

        async def chat_structured(self, **kwargs):
            started.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(run(synced, SlowLLM(), ScriptedExecutor([ok_result()]), cancellation=cancellation))
    await started.wait()
    await cancellation.cancel()
    out = await asyncio.wait_for(task, 5)
    assert out.failure_code == "execution_cancelled"


async def test_cancelled_between_attempts_skips_the_repair_call(synced):
    cancellation = PostgresExecutionCancellation()

    class CancellingExecutor(ScriptedExecutor):
        async def __call__(self, **kwargs):
            result = await super().__call__(**kwargs)
            await cancellation.cancel()
            return result

    llm = StubLLM([sql_env(BAD), sql_env(GOOD)])
    out = await run(synced, llm, CancellingExecutor([rejection(RejectionCode.PARSE_ERROR)]), cancellation=cancellation)
    assert out.failure_code == "execution_cancelled" and len(llm.calls) == 1


async def test_a_clarification_is_plain_text_and_at_most_300_characters(synced):
    markup = {**CLARIFY, "clarification": "Which <b>year</b>? [docs](http://x.y)"}
    out = await run(synced, StubLLM([markup]), ScriptedExecutor([ok_result()]))
    assert out.kind == "clarification" and out.user_message == "Which year? docs"
    too_long = await run(synced, StubLLM([{**CLARIFY, "clarification": "x" * 301}]), ScriptedExecutor([ok_result()]))
    assert too_long.failure_code == "envelope_invalid"
    from app.services.nl_sql.prompt import instructions

    leak = await run(synced, StubLLM([{**CLARIFY, "clarification": instructions()[100:200]}]), ScriptedExecutor([ok_result()]))
    assert leak.failure_code == "instruction_leak"
