"""SPEC-09 L1-17 (sibling sweep): a personal value or a placeholder is looked for in ALL four channels of a consent-on
turn: the model prompt, the logs, the SSE frames and the persisted JSON. The rows themselves may appear only in
``postgres_result`` (the owner is the viewer)."""
from __future__ import annotations

import json
import logging

import structlog

from app.services import column_privacy
from app.services.sql_execution import SqlExecutionResult, SqlResultColumn
from tests.nl_sql.chat_harness import drive, head_snapshot
from tests.nl_sql.helpers import ok_result, sql_env
from tests.nl_sql.pass2_helpers import T
from app.services.postgres_query_executor import PostgresResultValue

import uuid

NAMES = ["Asha Rao", "Ben Cole", "Chitra Menon"]
EMAILS = ["asha.rao@example.com", "ben.cole@example.com", "chitra@example.org"]
SQL = "SELECT c.full_name AS full_name, c.email AS email, c.city AS city FROM sales.customers AS c"
SPEC = {
    "display": "table", "encodings": {"x": None, "y": [], "series": None, "label": None, "value": None},
    "table_columns": [], "answer": "Listed {{rowcount}} customers: see the table.", "takeaways": [], "caveats": [],
}


def personal_result(snapshot_id, source_id):
    rows = tuple(
        (T(n), T(e), T("Pune")) for n, e in zip(NAMES, EMAILS)
    )
    return SqlExecutionResult(
        run_id=uuid.uuid4(), status="succeeded", sql=SQL, row_count=len(rows), rows=rows,
        columns=(SqlResultColumn(label="full_name", pg_type="text"), SqlResultColumn(label="email", pg_type="text"),
                 SqlResultColumn(label="city", pg_type="text")),
        source_id=source_id, snapshot_id=snapshot_id,
    )


async def run_turn(synced, monkeypatch, consent: bool):
    async with synced.factory() as db:
        await column_privacy.update_ai_settings(db, source_id=synced.source_id, owner_user_id=synced.owner_id, result_rows_to_model=consent)
    snapshot = await head_snapshot(synced)
    return await drive(
        synced, monkeypatch, question="list our customers", responses=[sql_env(SQL), SPEC],
        script=[personal_result(snapshot, synced.source_id)],
    )


def sweep(run, captured_logs):
    values = NAMES + EMAILS
    placeholders = ["full_name_1", "full_name_2", "full_name_3", "email_1", "email_2", "email_3"]
    prompt = run.llm.calls[1]["user"]
    sse = [e for e in run.events if e.event.value != "postgres_result"]
    sse_text = json.dumps([e.data for e in sse], default=str)
    record = run.assistant.nl_sql.model_dump_json()
    logs = json.dumps(captured_logs, default=str)
    return prompt, sse_text, record, logs, values, placeholders


async def test_consent_on_no_personal_value_in_any_channel(synced, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG), structlog.testing.capture_logs() as captured:
        run = await run_turn(synced, monkeypatch, consent=True)
    prompt, sse_text, record, logs, values, placeholders = sweep(run, captured)
    log_text = logs + caplog.text
    for value in values:
        assert value not in prompt, "prompt"
        assert value not in sse_text, "SSE frames outside postgres_result"
        assert value not in record, "persisted nl_sql"
        assert value not in log_text, "logs"
        assert value not in run.assistant.content, "the stored answer text"
    for placeholder in placeholders:
        assert placeholder in prompt                                         # the model did see placeholders ...
        assert placeholder not in sse_text and placeholder not in record     # ... and they never leave the prompt
        assert placeholder not in log_text and placeholder not in run.assistant.content
    # the owner does see the rows, in postgres_result only
    shown = run.data("postgres_result")[0]["postgres_result"]["rows"]
    assert shown[0][0]["value"] == NAMES[0]
    assert NAMES[0] in run.assistant.postgres_result.model_dump_json()
    assert run.assistant.nl_sql.presentation.pass2.masked_columns == ["full_name", "email"]
    assert run.assistant.nl_sql.presentation.pass2.rows_sent == 3


async def test_consent_off_no_row_value_in_any_channel(synced, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG), structlog.testing.capture_logs() as captured:
        run = await run_turn(synced, monkeypatch, consent=False)
    prompt, sse_text, record, logs, values, placeholders = sweep(run, captured)
    log_text = logs + caplog.text
    for value in values + ["Pune"]:
        assert value not in prompt, "prompt"
        assert value not in sse_text and value not in record and value not in log_text
    assert not any(p in prompt or p in sse_text or p in record for p in placeholders)
    assert "row values are not available" in prompt
    assert run.assistant.nl_sql.presentation.pass2.rows_sent == 0
