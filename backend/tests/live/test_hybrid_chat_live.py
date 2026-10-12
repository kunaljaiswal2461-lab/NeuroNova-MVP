"""SPEC-09 L2: the whole hybrid database chat turn against the real fixture, with a scripted model (no key).

The pass-1 SQL of each scripted answer is a golden reference query, so the PIPELINE (validator, executor, repair,
privacy, pass 2, persistence, SSE order) is what is tested live. Run inside the runner container:

    docker compose -f backend/evals/fixture/compose.yml run --rm runner sh -c "NN_LIVE_PG=1 pytest -q -m live_pg tests/live/test_hybrid_chat_live.py"
"""
from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational import postgres_chat_agent as agent
from agentic_engine.conversational.models import ConversationMode
from agentic_engine.conversational.postgres_chat_agent import stream_postgres_chat_turn
from agentic_engine.conversational.postgres_planner import PostgresPlanningResult
from agentic_engine.llm_engine.scripted_client import ScriptedLLM
from app.db.models.postgres_query_run import PostgresQueryRun
from app.db.models.source import DataSource
from app.schemas.source_schemas import MetricDefinitionWriteRequest
from app.services import column_privacy, metric_service
from app.services.nl_sql.prompt import PASS2_CANARY
from app.services.sql_execution import execute_validated_sql
from app.services.sql_guard import load_catalog_scope
from evals import pgrun
from evals.bootstrap import bootstrap
from evals.candidates.base import CandidateOutcome
from evals.candidates.nl_sql import decode_sql_result
from evals.candidates.stage4c import decode_value
from evals.grader import ResultColumn, ResultSet, Verdict, grade
from evals.models import Expectation

pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio(loop_scope="module")]

NO = {"x": None, "y": [], "series": None, "label": None, "value": None}


def sql_env(sql, intent="table", **enc):
    return {
        "kind": "sql", "sql": sql, "assumptions": ["scripted for the live test"], "tables_used": [],
        "output_intent": {"type": intent, "encodings": {**NO, **enc}},
        "clarification": None, "reason_code": None, "reason": None,
    }


def spec(display="table", answer="Done.", **changes):
    base = {"display": display, "encodings": {**NO}, "table_columns": [], "answer": answer, "takeaways": [], "caveats": []}
    base.update(changes)
    return base


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ctx():
    c = await bootstrap()
    yield c
    await c.dispose()


@pytest.fixture
def script(tmp_path):
    def build(*lines) -> ScriptedLLM:
        path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        return ScriptedLLM(path)

    return build


def p1(match, response, **extra):
    return {"schema": "pass1_envelope", "match": match, "response": response, **extra}


def p2(match, response, **extra):
    return {"schema": "pass2_spec", "match": match, "response": response, **extra}


class Turn:
    def __init__(self, events, messages, llm):
        self.events, self.messages, self.llm = events, messages, llm

    @property
    def types(self):
        return [e.event.value for e in self.events]

    def data(self, name):
        return [e.data for e in self.events if e.event.value == name]

    @property
    def assistant(self):
        return [m for m in self.messages if m.role.value == "ASSISTANT"][-1]

    def pass2_prompts(self):
        return [c["user"] for c in self.llm.calls if c["schema_name"] == "pass2_spec"]


async def new_session(ctx):
    async with ctx.session_factory() as db:
        return await manager.create_database_session_for_access(
            session_db=db, source_id=ctx.source_id, mode=ConversationMode.AUTO,
            access=manager.ConversationAccess.for_user(ctx.user_id),
        )


async def turn(ctx, question, llm, *, session=None, sql_execute=None, settings=None) -> Turn:
    session = session or await new_session(ctx)
    events = []
    async with ctx.session_factory() as db:
        async for event in stream_postgres_chat_turn(
            session_id=session.session_id, user_message=question, session_db=db, owner_user_id=ctx.user_id,
            settings=settings or ctx.settings, llm=llm, session_factory=ctx.session_factory, sql_execute=sql_execute,
            sql_execute_kwargs={"adapter_factory": lambda _s: ctx.query_adapter_factory()},
        ):
            events.append(event)
    async with ctx.session_factory() as db:
        messages = await manager.get_recent_messages(db, session.session_id)
    return Turn(events, messages, llm)


async def reference(sql):
    conn = await pgrun.connect_grader()
    try:
        return await pgrun.run_result(conn, sql, max_rows=1000)
    finally:
        await conn.close()


def event_result_set(turn_: Turn) -> ResultSet:
    result = turn_.data("postgres_result")[0]["postgres_result"]
    columns = tuple(ResultColumn(c["alias"], c["actual_postgres_type"]) for c in result["columns"])
    rows = tuple(tuple(decode_value(cell["kind"], cell["value"]) for cell in row) for row in result["rows"])
    return ResultSet(columns=columns, rows=rows, complete=result["complete"])


async def assert_equals_grader_run(turn_: Turn, sql: str, *, order_matters=False):
    ref = await reference(sql)
    verdict = grade(Expectation(kind="answer", reference_sql=sql, order_matters=order_matters), ref,
                    CandidateOutcome("answer", result=event_result_set(turn_)))
    assert verdict.verdict in (Verdict.PASS, Verdict.PASS_EXTRA_COLUMNS), verdict


async def set_consent(ctx, on: bool):
    async with ctx.session_factory() as db:
        await column_privacy.update_ai_settings(db, source_id=ctx.source_id, owner_user_id=ctx.user_id, result_rows_to_model=on)


async def set_privacy(ctx, table, column, state):
    async with ctx.session_factory() as db:
        await column_privacy.update_column_privacy(
            db, source_id=ctx.source_id, owner_user_id=ctx.user_id, changes=[(("shop", table, column), state)])


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def reset_settings(ctx):
    yield
    await set_consent(ctx, False)
    for table, column in (("refunds", "reason"), ("customers", "segment"), ("customers", "full_name")):
        await set_privacy(ctx, table, column, None)


# ── 1. end to end, one test per kind of answer ────────────────────────────────

async def test_single_number(ctx, script):
    q = "live: how many orders in total"
    llm = script(
        p1(q, sql_env("SELECT count(*) FROM shop.orders", "kpi", value="count")),
        p2(q, spec("kpi", "There are {{stat:count.sum}} orders.", encodings={**NO, "value": "count"})),
    )
    t = await turn(ctx, q, llm)
    assert t.types == ["start", "planning", "sql_generated", "executing", "postgres_result", "presentation", "token", "done"]
    await assert_equals_grader_run(t, "SELECT count(*) FROM shop.orders")
    total = (await reference("SELECT count(*) FROM shop.orders")).rows[0][0]
    assert t.data("presentation")[0]["answer"] == f"There are {total} orders."
    record = t.assistant.nl_sql
    assert record.outcome == "answer" and record.sql.lower().startswith("select count(*)")
    assert record.prompt_versions == {"pass1": "pass1-v2", "pass2": "pass2-v1"} and record.presentation.display == "kpi"
    assert record.presentation.pass2.consent is False and "postgres_result" not in json.dumps(record.model_dump())
    stored = t.assistant.postgres_result
    assert stored is not None and "select" not in stored.model_dump_json().lower()                     # I14
    async with ctx.session_factory() as db:
        runs = (await db.execute(select(PostgresQueryRun))).scalars().all()
    assert all(not hasattr(r, "sql") for r in runs)                                                  # the run row has no SQL column


async def test_grouped_bar(ctx, script):
    q = "live: orders per channel and status"
    sql = "SELECT channel, status, count(*) AS n FROM shop.orders GROUP BY channel, status"
    llm = script(
        p1(q, sql_env(sql, "grouped_bar", x="channel", series="status", y=["n"])),
        p2(q, spec("grouped_bar", "Orders by channel and status.", encodings={**NO, "x": "channel", "series": "status", "y": ["n"]})),
    )
    t = await turn(ctx, q, llm)
    assert t.data("presentation")[0]["display"] == "grouped_bar" and t.data("presentation")[0]["fallback"] is None
    await assert_equals_grader_run(t, sql)


async def test_multi_line(ctx, script):
    q = "live: monthly orders per channel"
    sql = ("SELECT date_trunc('month', ordered_at AT TIME ZONE 'Asia/Kolkata')::date AS month, channel, count(*) AS n "
           "FROM shop.orders GROUP BY 1, 2")
    llm = script(
        p1(q, sql_env(sql, "multi_line", x="month", series="channel", y=["n"])),
        p2(q, spec("multi_line", "Monthly orders per channel.", encodings={**NO, "x": "month", "series": "channel", "y": ["n"]})),
    )
    t = await turn(ctx, q, llm)
    assert t.data("presentation")[0]["display"] == "multi_line"
    await assert_equals_grader_run(t, sql)


async def test_a_pie_with_seven_categories_is_downgraded(ctx, script):
    q = "live: refunds per reason as a pie"
    sql = "SELECT reason, count(*) AS n FROM shop.refunds GROUP BY reason"
    llm = script(
        p1(q, sql_env(sql, "pie", label="reason", value="n")),
        p2(q, spec("pie", "Refunds by reason.", encodings={**NO, "label": "reason", "value": "n"})),
    )
    t = await turn(ctx, q, llm)
    presentation = t.data("presentation")[0]
    assert presentation["display"] in ("table", "bar") and presentation["fallback"].startswith("display_downgraded:pie->")
    assert any("does not fit pie" in c for c in presentation["caveats"])
    await assert_equals_grader_run(t, sql)


async def test_write_request_reaches_no_model(ctx, script):
    llm = script()
    t = await turn(ctx, "delete every refund please", llm)
    assert t.types == ["start", "planning", "unsupported", "token", "done"] and llm.calls == []
    assert t.assistant.unsupported


async def test_clarification(ctx, script):
    q = "live: show me revenue"
    llm = script(p1(q, {
        "kind": "clarification", "sql": None, "assumptions": [], "tables_used": [], "output_intent": None,
        "clarification": "Do you mean gross or net revenue?", "reason_code": None, "reason": None}))
    t = await turn(ctx, q, llm)
    assert t.types == ["start", "planning", "clarification", "token", "done"]
    assert t.assistant.content == "Do you mean gross or net revenue?" and t.assistant.nl_sql.outcome == "clarification"


async def create_metric(ctx, name="Live paid revenue"):
    async with ctx.session_factory() as session:
        head = ctx.snapshot_id
        scope = await load_catalog_scope(session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=head)
        orders = next(t for t in scope.tables if t.name == "orders")
        col = {c.name: c.column_id for c in orders.columns}
        request = MetricDefinitionWriteRequest.model_validate({
            "name": name, "owner_confirmed": True, "description": "Gross value of paid orders", "snapshot_id": head,
            "asset_id": orders.asset_id, "aggregation": {"operation": "sum", "column_id": col["total_amount"]},
            "fixed_filters": [{"column_id": col["status"], "operator": "eq", "value": {"kind": "text", "value": "PAID"}}],
            "unit_label": "INR", "default_date_column_id": col["ordered_at"], "default_time_zone": "Asia/Kolkata",
        })
        await metric_service.create_metric_definition_for_owner(
            session, source_id=ctx.source_id, owner_user_id=ctx.user_id, request=request, settings=ctx.settings)


async def test_a_named_metric_keeps_the_typed_path_and_a_half_match_falls_through(ctx, script, monkeypatch):
    await create_metric(ctx)
    planned = []

    async def typed_clarifies(**kwargs):
        planned.append(kwargs["question"])
        return PostgresPlanningResult(kind="clarification", message="Which period?")

    monkeypatch.setattr(agent, "plan_postgres_question", typed_clarifies)
    q = "live paid revenue please"
    llm = script(p1("live paid revenue", sql_env("SELECT 1 FROM shop.orders LIMIT 1")))
    t = await turn(ctx, q, llm)
    assert planned == [q] and t.types[-3:] == ["clarification", "token", "done"] and llm.calls == []     # R3: typed, no model

    async def typed_cannot(**kwargs):
        planned.append(kwargs["question"])
        return PostgresPlanningResult(kind="unsupported", message="not an approved relationship")

    monkeypatch.setattr(agent, "plan_postgres_question", typed_cannot)
    q2 = "live paid revenue by region"
    sql = "SELECT r.name, sum(o.total_amount) AS v FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id JOIN shop.regions r ON r.id = c.region_id WHERE o.status = 'PAID' GROUP BY r.name"
    llm2 = script(p1("live paid revenue by region", sql_env(sql, "bar", x="name", y=["v"])),
                  p2("live paid revenue by region", spec("bar", "Revenue by region.", encodings={**NO, "x": "name", "y": ["v"]})))
    t2 = await turn(ctx, q2, llm2)
    assert planned.count(q2) == 1 and t2.assistant.nl_sql.route == "sql_after_metric_fallthrough"      # R3a, exactly once
    await assert_equals_grader_run(t2, sql)


# ── 2. repair, live ───────────────────────────────────────────────────────────

async def test_a_misspelled_column_is_repaired_once(ctx, script):
    q = "live: repair a misspelled column"
    good = "SELECT count(*) AS n FROM shop.orders"
    llm = script(
        p1(q, sql_env("SELECT count_of_orders FROM shop.orders", "kpi", value="n"), brief=False),
        p1(q, sql_env(good, "kpi", value="n"), brief=True),
        p2(q, spec("kpi", "Total {{stat:n.sum}}.", encodings={**NO, "value": "n"})),
    )
    t = await turn(ctx, q, llm)
    assert t.types == ["start", "planning", "sql_generated", "repairing", "sql_generated", "executing",
                       "postgres_result", "presentation", "token", "done"]
    assert t.assistant.nl_sql.repair.reason_code == "column_not_found"
    assert len([c for c in llm.calls if c["schema_name"] == "pass1_envelope"]) == 2
    brief = [c["user"] for c in llm.calls if "Your previous answer could not be used." in c["user"]][0]
    assert "count_of_orders" in brief and "problem: column_not_found" in brief
    await assert_equals_grader_run(t, good)


async def test_a_postgres_grouping_error_is_repaired_once(ctx, script):
    q = "live: repair a grouping error"
    bad = "SELECT status, channel, count(*) AS n FROM shop.orders GROUP BY status"
    good = "SELECT status, channel, count(*) AS n FROM shop.orders GROUP BY status, channel"
    llm = script(
        p1(q, sql_env(bad, "table"), brief=False),
        p1(q, sql_env(good, "table"), brief=True),
        p2(q, spec("table", "Orders by status and channel.")),
    )
    t = await turn(ctx, q, llm)
    assert t.types[:5] == ["start", "planning", "sql_generated", "executing", "repairing"]
    assert t.assistant.nl_sql.repair.reason_code == "query_error:42803"
    brief = [c["user"] for c in llm.calls if "Your previous answer could not be used." in c["user"]][0]
    assert "Grouping error" in brief and "must appear in the GROUP BY" not in brief and "42803" not in brief   # ours, not Postgres's
    await assert_equals_grader_run(t, good)


async def test_a_repair_that_fails_again_gives_the_rephrase_message(ctx, script):
    q = "live: repair fails twice"
    bad = "SELECT status, channel, count(*) AS n FROM shop.orders GROUP BY status"
    llm = script(p1(q, sql_env(bad, "table")))
    t = await turn(ctx, q, llm)
    assert t.types[-3:] == ["error", "token", "done"] and len([c for c in llm.calls if c["schema_name"] == "pass1_envelope"]) == 2
    assert t.assistant.content.startswith("I couldn't write a working query") and t.assistant.degraded


# ── 3. personal data ──────────────────────────────────────────────────────────

async def personal_values():
    conn = await pgrun.connect_grader()
    try:
        rows = await conn.fetch("SELECT full_name, email, phone FROM shop.customers")
    finally:
        await conn.close()
    return {v for row in rows for v in row if isinstance(v, str) and len(v) >= 6}


async def test_consent_on_masks_personal_columns_in_the_prompt(ctx, script):
    await set_consent(ctx, True)
    q = "live: customer names and emails"
    llm = script(
        p1(q, sql_env("SELECT full_name, email, city FROM shop.customers LIMIT 60", "table")),
        p2(q, spec("table", "Here are customers.")),
    )
    t = await turn(ctx, q, llm)
    prompt = t.pass2_prompts()[0]
    values = await personal_values()
    assert len(values) > 5_000 and not [v for v in values if v in prompt]                            # I5: none of 5,000+ values
    assert "full_name_1" in prompt and "email_1" in prompt and "rows shown: 50 of 60" in prompt
    assert t.assistant.nl_sql.presentation.pass2.masked_columns == ["full_name", "email"]
    assert any("hidden from the AI model" in c for c in t.data("presentation")[0]["caveats"])
    shown = t.data("postgres_result")[0]["postgres_result"]["rows"][0][0]["value"]
    assert shown in values                                                                           # the owner sees the real value


async def test_consent_off_sends_no_row_value_at_all(ctx, script):
    await set_consent(ctx, False)
    q = "live: customer cities, consent off"
    llm = script(p1(q, sql_env("SELECT full_name, email, city FROM shop.customers LIMIT 60", "table")),
                 p2(q, spec("table", "Here are customers.")))
    t = await turn(ctx, q, llm)
    prompt = t.pass2_prompts()[0]
    values = await personal_values()
    cities = {c for (c,) in ((r[0],) for r in (await reference("SELECT DISTINCT city FROM shop.customers LIMIT 20")).rows)}
    assert not [v for v in values if v in prompt] and "row values are not available" in prompt and "row 0:" not in prompt
    assert t.assistant.nl_sql.presentation.pass2.consent is False and t.assistant.nl_sql.presentation.pass2.rows_sent == 0
    assert any("did not see row values" in c for c in t.data("presentation")[0]["caveats"])
    assert not any(f'"{city}"' in prompt for city in cities)


# ── 4. the hostile row value ──────────────────────────────────────────────────

async def test_a_hostile_cell_stays_inside_a_fenced_literal_and_html_is_stripped(ctx, script):
    await set_consent(ctx, True)
    await set_privacy(ctx, "refunds", "reason", "not_personal")        # free-text by name, so personal unless the owner says no
    q = "live: the planted refund reason"
    sql = "SELECT reason FROM shop.refunds WHERE reason ILIKE '%disregard your rules%' LIMIT 3"
    hostile = (await reference(sql)).rows
    assert hostile, "the fixture plants a refund reason that tries to steer the model"
    value = hostile[0][0]
    llm = script(
        p1(q, sql_env(sql, "table")),
        p2(q, spec("table", f"<img src=x onerror=alert(1)> **Reason**: [click](http://evil.example) {value}")),
    )
    t = await turn(ctx, q, llm)
    prompt = t.pass2_prompts()[0]
    lines = [l for l in prompt.splitlines() if "disregard your rules" in l]
    assert lines and all(l.startswith("  row ") and l.strip().endswith("]") for l in lines)             # only inside a row literal
    presentation = t.data("presentation")[0]
    assert "<" not in presentation["answer"] and "](" not in presentation["answer"] and "http" not in presentation["answer"]
    assert t.assistant.nl_sql.presentation.pass2.consent is True


# ── 5. the revocation race ────────────────────────────────────────────────────

async def test_revoking_the_source_between_execution_and_pass_2_sends_no_row(ctx, script):
    await set_consent(ctx, True)
    q = "live: revocation race"
    llm = script(p1(q, sql_env("SELECT city FROM shop.customers LIMIT 5", "table")), p2(q, spec("table", "Cities.")))

    async def revoking(**kwargs):
        result = await execute_validated_sql(**kwargs)
        async with ctx.session_factory() as db:                       # the owner changes the source right after the query ran
            await db.execute(update(DataSource).where(DataSource.id == ctx.source_id).values(
                configuration_generation=DataSource.configuration_generation + 1))
            await db.commit()
        return result

    t = await turn(ctx, q, llm, sql_execute=revoking)
    assert len([c for c in llm.calls if c["schema_name"] == "pass2_spec"]) == 0                      # no row went to the model
    assert "postgres_result" not in t.types and t.data("error")[0]["code"] == "access_revoked"
    assert t.assistant.degraded_reason == "access_revoked" and t.assistant.postgres_result is None


# ── 6. the settings take effect on the next turn ──────────────────────────────

async def test_a_column_override_changes_the_next_turns_masking(ctx, script):
    await set_consent(ctx, True)
    sql = "SELECT segment, count(*) AS n FROM shop.customers GROUP BY segment"

    q1 = "live: segments before"
    t1 = await turn(ctx, q1, script(p1(q1, sql_env(sql, "table")), p2(q1, spec("table", "Segments."))))
    assert '"Retail"' in t1.pass2_prompts()[0] and t1.assistant.nl_sql.presentation.pass2.masked_columns == []

    await set_privacy(ctx, "customers", "segment", "personal")
    q2 = "live: segments after"
    t2 = await turn(ctx, q2, script(p1(q2, sql_env(sql, "table")), p2(q2, spec("table", "Segments."))))
    assert '"Retail"' not in t2.pass2_prompts()[0] and "segment_1" in t2.pass2_prompts()[0]
    assert t2.assistant.nl_sql.presentation.pass2.masked_columns == ["segment"]


async def test_follow_up_question_carries_the_previous_sql(ctx, script):
    session = await new_session(ctx)
    q1, q2 = "live: orders by status", "live: only for the app channel"
    sql1 = "SELECT status, count(*) AS n FROM shop.orders GROUP BY status"
    sql2 = "SELECT status, count(*) AS n FROM shop.orders WHERE channel = 'app' GROUP BY status"
    llm = script(
        p1(q1, sql_env(sql1, "bar", x="status", y=["n"])), p2(q1, spec("bar", "By status.", encodings={**NO, "x": "status", "y": ["n"]})),
        p1(q2, sql_env(sql2, "bar", x="status", y=["n"])), p2(q2, spec("bar", "App only.", encodings={**NO, "x": "status", "y": ["n"]})),
    )
    await turn(ctx, q1, llm, session=session)
    t2 = await turn(ctx, q2, llm, session=session)
    second_prompt = [c["user"] for c in llm.calls if c["schema_name"] == "pass1_envelope"][-1]
    assert "Earlier in this conversation:" in second_prompt
    assert '  sql: "' in second_prompt and "group by status" in second_prompt.lower()                    # the first turn's SQL
    assert "  assumptions: [" in second_prompt and 'output_type: "bar"' in second_prompt
    await assert_equals_grader_run(t2, sql2)


async def test_the_stage_4c_typed_only_mode_is_a_real_kill_switch(ctx, script, monkeypatch):
    settings = ctx.settings.model_copy(update={"nl_sql_route_mode": "typed_only"})
    seen = []

    async def typed(**kwargs):
        seen.append(kwargs["question"])
        return PostgresPlanningResult(kind="unsupported", message="typed path only")

    monkeypatch.setattr(agent, "plan_postgres_question", typed)
    llm = script()
    t = await turn(ctx, "live: anything at all", llm, settings=settings)
    assert seen == ["live: anything at all"] and llm.calls == [] and t.types[-3:] == ["unsupported", "token", "done"]
