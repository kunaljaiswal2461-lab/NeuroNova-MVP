"""SPEC-09 acceptance: the browser's round trip over HTTP against the real fixture, with the scripted stub model.

The server is the real FastAPI app (real JWT, real app database, real executor); ``NL_SQL_STUB_LLM_PATH`` replaces the
OpenAI client. What the browser would send and receive is exactly what is asserted here: the SSE frames in order, and the
stored message as the messages endpoint returns it (``nl_sql`` included), plus the settings endpoints.
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models.user import User
from evals.bootstrap import bootstrap
from main import create_app

pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio(loop_scope="module")]

NO = {"x": None, "y": [], "series": None, "label": None, "value": None}


def frames(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ctx():
    c = await bootstrap()
    yield c
    await c.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def api(ctx, tmp_path_factory):
    # The production connection policy refuses Docker's private addresses; the fixture's documented substitution is the
    # disposable-test policy, put in at the one place the application builds its query adapter.
    from app.services import sql_execution

    original = sql_execution._build_adapter
    sql_execution._build_adapter = lambda _settings: ctx.query_adapter_factory()
    script = tmp_path_factory.mktemp("stub") / "script.jsonl"
    sql = "SELECT channel, count(*) AS n FROM shop.orders GROUP BY channel"
    script.write_text("\n".join(json.dumps(line) for line in [
        {"schema": "pass1_envelope", "match": "orders per channel", "response": {
            "kind": "sql", "sql": sql, "assumptions": ["All orders, every status"], "tables_used": ["shop.orders"],
            "output_intent": {"type": "bar", "encodings": {**NO, "x": "channel", "y": ["n"]}},
            "clarification": None, "reason_code": None, "reason": None}},
        {"schema": "pass2_spec", "match": "orders per channel", "response": {
            "display": "bar", "encodings": {**NO, "x": "channel", "y": ["n"]},
            "table_columns": [{"column": "channel", "label": "Channel"}, {"column": "n", "label": "Orders"}],
            "answer": "Orders are split across {{stat:n.count}} channels.", "takeaways": [], "caveats": []}},
    ]), encoding="utf-8")
    settings = ctx.settings.model_copy(update={"nl_sql_stub_llm_path": str(script)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with ctx.session_factory() as db:
        user = await db.get(User, ctx.user_id)
    headers = {"Authorization": "Bearer " + create_access_token(
        str(user.id), user.email, user.role.value, ctx.settings.jwt_signing_key, 30)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        yield client
    app.dependency_overrides.clear()
    sql_execution._build_adapter = original


async def test_question_to_stored_answer(ctx, api):
    created = await api.post(f"/api/v1/sources/{ctx.source_id}/chat/sessions", json={"mode": "AUTO"})
    assert created.status_code == 201
    session_id = created.json()["session"]["session_id"]

    sent = await api.post(f"/api/v1/chat/sessions/{session_id}/message", json={"message": "Show orders per channel"})
    assert sent.status_code == 200
    events = frames(sent.text)
    assert [e for e, _ in events] == [
        "start", "planning", "sql_generated", "executing", "postgres_result", "presentation", "token", "done"]
    by = dict(events)
    assert by["sql_generated"]["sql"].lower().startswith("select channel") and by["sql_generated"]["attempt"] == 1
    presentation = by["presentation"]
    assert presentation["display"] == "bar" and presentation["answer"] == "Orders are split across 4 channels."
    assert [c["label"] for c in presentation["table_columns"]] == ["Channel", "Orders"]
    result = by["postgres_result"]["postgres_result"]
    assert [c["alias"] for c in result["columns"]] == ["channel", "n"] and result["columns"][1]["actual_postgres_type"] == "int8"
    assert len(result["rows"]) == 4 and all(cell["kind"] in ("text", "integer") for row in result["rows"] for cell in row)

    stored = await api.get(f"/api/v1/chat/sessions/{session_id}/messages")
    messages = stored.json()["messages"]
    assert [m["role"] for m in messages] == ["USER", "ASSISTANT"] and messages[0]["nl_sql"] is None
    record = messages[1]["nl_sql"]
    assert record["version"] == 1 and record["outcome"] == "answer" and record["route"] == "sql"
    assert record["assumptions"] == ["All orders, every status"] and record["repair"] is None
    assert record["presentation"]["answer_template"] == "Orders are split across {{stat:n.count}} channels."
    assert messages[1]["content"] == presentation["answer"] and messages[1]["postgres_result"]["status"] == "succeeded"
    assert "SELECT" not in json.dumps(messages[1]["postgres_result"]).upper()          # I14: no SQL in the result blob

    again = await api.post(f"/api/v1/chat/sessions/{session_id}/message", json={"message": "Show orders per channel"})
    assert again.status_code == 200                                                    # the slot was freed after the first turn


async def test_a_question_the_stub_cannot_answer_ends_cleanly(ctx, api):
    session_id = (await api.post(f"/api/v1/sources/{ctx.source_id}/chat/sessions", json={"mode": "AUTO"})).json()["session"]["session_id"]
    sent = await api.post(f"/api/v1/chat/sessions/{session_id}/message", json={"message": "something unscripted"})
    events = frames(sent.text)
    assert [e for e, _ in events][-3:] == ["error", "token", "done"]
    assert dict(events)["error"]["code"] == "llm_unavailable"
    assert dict(events)["error"]["message"] == "The assistant model is unavailable right now. Please try again shortly."


async def test_write_requests_are_refused_over_http_with_no_model(ctx, api):
    session_id = (await api.post(f"/api/v1/sources/{ctx.source_id}/chat/sessions", json={"mode": "AUTO"})).json()["session"]["session_id"]
    sent = await api.post(f"/api/v1/chat/sessions/{session_id}/message", json={"message": "drop table orders"})
    assert [e for e, _ in frames(sent.text)] == ["start", "planning", "unsupported", "token", "done"]


async def test_the_ai_settings_endpoints_over_http(ctx, api):
    url = f"/api/v1/sources/{ctx.source_id}/ai-settings"
    assert (await api.get(url)).json()["result_rows_to_model"] is False
    assert (await api.put(url, json={"result_rows_to_model": True})).json()["result_rows_to_model"] is True
    rows = (await api.get(f"/api/v1/sources/{ctx.source_id}/column-privacy")).json()
    emails = [r for r in rows if r["column"] == "email" and r["table"] == "customers"]
    assert emails and emails[0]["suggested_personal"] is True and emails[0]["effective_personal"] is True
    assert (await api.put(url, json={"result_rows_to_model": False})).json()["result_rows_to_model"] is False
