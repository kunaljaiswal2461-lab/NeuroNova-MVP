"""SPEC-08 L1-2 (write guard, I1) and L1-3 (clock rewrite)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.nl_sql import rewrite_clock
from tests.nl_sql.conftest import REF_TIME, StubLLM, run_pass1

WRITES = [
    "delete all orders",
    "DROP TABLE sales.orders",
    "please update the status of order 5 to paid",
    "UPDATE sales.orders SET status = 'x'",
    "insert a new customer named Asha",
    "truncate the orders table",
    "alter table sales.orders add column foo int",
    "remove all cancelled orders from the database",
    "create table backup as select * from sales.orders",
    "Delete from sales.customers where city = 'Pune'",
    "grant select on sales.orders to public",
    "drop the customers table please",
]
PROSE = [
    "how many deleted orders were there last month",
    "show orders that were updated after signup",
    "which customers were removed from the newsletter",
    "what is the average order value",
    "list products created in May",
]


class _NeverCalled:
    def __call__(self):
        raise AssertionError("the catalog must not be read for a write request")


@pytest.mark.parametrize("question", WRITES)
async def test_write_request_is_refused_without_model_or_catalog(world, question):
    llm = StubLLM()
    result = await run_pass1(
        type("W", (), {"factory": _NeverCalled(), "settings": world.settings,
                       "owner_id": world.owner_id, "source_id": world.source_id})(),
        llm, question,
    )
    assert llm.calls == []                                   # I1
    assert result.failure_code is None
    assert result.envelope.kind == "unsupported" and result.envelope.reason_code == "write_request"
    assert result.llm_call is None


@pytest.mark.parametrize("question", PROSE)
async def test_analytical_prose_is_not_a_write_request(synced, question):
    llm = StubLLM()
    result = await run_pass1(synced, llm, question)
    assert len(llm.calls) == 1
    assert result.envelope.kind == "sql"


def test_phrasing_count_meets_spec():
    assert len(WRITES) + len(PROSE) >= 15


# ── clock rewrite ────────────────────────────────────────────────────────────

TS = "CAST('2026-06-15T10:00:00+05:30' AS TIMESTAMPTZ)"


@pytest.mark.parametrize(
    "call",
    ["now()", "current_timestamp", "CURRENT_TIMESTAMP", "transaction_timestamp()", "statement_timestamp()"],
)
def test_each_timestamp_function_becomes_the_reference_literal(call):
    sql, changed = rewrite_clock(f"SELECT {call} AS t FROM sales.orders", REF_TIME, "Asia/Kolkata")
    assert changed and sql == f"SELECT {TS} AS t FROM sales.orders"


def test_localtimestamp_becomes_a_plain_timestamp_of_the_local_wall_time():
    # SPEC-09 W0.3: localtimestamp is `timestamp` (no zone) in PostgreSQL, so it must not turn into a TIMESTAMPTZ.
    sql, changed = rewrite_clock("SELECT localtimestamp AS t FROM sales.orders", REF_TIME, "Asia/Kolkata")
    assert changed and sql == "SELECT CAST('2026-06-15T10:00:00' AS TIMESTAMP) AS t FROM sales.orders"


def test_current_date_becomes_a_date_literal():
    sql, changed = rewrite_clock("SELECT current_date AS d", REF_TIME, "Asia/Kolkata")
    assert changed and sql == "SELECT CAST('2026-06-15' AS DATE) AS d"


def test_current_date_uses_the_zone_date_not_the_utc_date():
    just_after_midnight_ist = datetime(2026, 6, 14, 20, 0, tzinfo=timezone.utc)      # 2026-06-15 01:30 in IST
    assert just_after_midnight_ist.date().isoformat() == "2026-06-14"
    sql, _ = rewrite_clock("SELECT current_date", just_after_midnight_ist, "Asia/Kolkata")
    assert "'2026-06-15'" in sql and "2026-06-14" not in sql


def test_reference_time_is_expressed_in_the_zone_whatever_zone_it_came_in():
    utc = datetime(2026, 6, 15, 4, 30, tzinfo=timezone.utc)
    sql, _ = rewrite_clock("SELECT now()", utc, "Asia/Kolkata")
    assert "2026-06-15T10:00:00+05:30" in sql


def test_clock_used_inside_a_filter_is_rewritten():
    sql, changed = rewrite_clock(
        "SELECT id FROM sales.orders WHERE ordered_at >= now() - interval '7 days'", REF_TIME, "Asia/Kolkata"
    )
    assert changed and "NOW" not in sql.upper() and TS in sql


@pytest.mark.parametrize(
    "text",
    [
        "SELECT clock_timestamp()",           # not on the allowlist: stays, so the validator rejects it
        "SELECT 1; DROP TABLE x",             # two statements: left to the validator
        "SELEC ((",                           # unparseable: left as it is
        "SELECT id FROM sales.orders",        # nothing to rewrite: byte-identical, not regenerated
        "select   id  from   sales.orders",
    ],
)
def test_other_text_passes_through_byte_for_byte(text):
    out, changed = rewrite_clock(text, REF_TIME, "Asia/Kolkata")
    assert out == text and not changed


def test_unparseable_sql_logs_nothing_from_sqlglot(caplog):
    with caplog.at_level("DEBUG"):
        rewrite_clock("SELEC ((now() secret-literal", REF_TIME, "Asia/Kolkata")
    assert "secret-literal" not in caplog.text


async def test_generate_records_the_rewrites(synced):
    from tests.nl_sql.conftest import envelope

    llm = StubLLM([envelope(sql="```sql\nSELECT now() FROM sales.orders\n```")])
    result = await run_pass1(synced, llm, "what is the time")
    assert result.rewrites == ("stripped_fence", "clock_literal")
    assert "NOW" not in result.envelope.sql.upper() and TS in result.envelope.sql


async def test_clock_rewrite_is_for_sql_only(synced):
    from tests.nl_sql.conftest import GOOD_SQL

    clarification = {**GOOD_SQL, "kind": "clarification", "sql": None, "output_intent": None,
                     "clarification": "Do you mean now() or current_date?"}
    result = await run_pass1(synced, StubLLM([clarification]), "when")
    assert result.rewrites == () and "now()" in result.envelope.clarification
