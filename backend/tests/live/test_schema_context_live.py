"""SPEC-06 L2: the schema context built from the real fixture catalog (run in the runner container)."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from app.schemas.source_schemas import MetricDefinitionWriteRequest
from app.services import metric_service
from app.services.schema_context import SchemaContextError, build_schema_context, quote_identifier
from app.services.schema_context.keywords import QUOTED_KEYWORDS
from app.services.sql_execution import execute_validated_sql
from app.services.sql_guard import Rejection, load_catalog_scope
from evals import pgrun
from evals.bootstrap import bootstrap
from tests.live.test_sql_execution_live import _resync, make_adapter_factory
from tests.schema_context.fixture_catalog import fixture_catalog
from app.services.schema_context import render_schema, O200K_COUNTER

pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio(loop_scope="module")]

GOLDEN = Path(__file__).resolve().parents[1] / "schema_context" / "snapshots" / "fixture_shop.txt"
OUT_OF_SCOPE = ("internal", "salaries", "audit_log", "customer_contacts", "rls_notes", "events_partitioned")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ctx():
    c = await bootstrap()
    yield c
    await c.dispose()


async def _build(c, snapshot_id=None):
    async with c.session_factory() as session:
        return await build_schema_context(
            session, owner_user_id=c.user_id, source_id=c.source_id,
            snapshot_id=snapshot_id or c.snapshot_id, settings=c.settings,
        )


async def test_L2_1_matches_golden_and_leaks_nothing(ctx):
    built = await _build(ctx)
    assert built.text == GOLDEN.read_text(encoding="utf-8")
    assert built.fingerprint == render_schema(fixture_catalog(), budget_tokens=8000, counter=O200K_COUNTER).fingerprint
    assert not built.skipped and not built.dropped
    for word in OUT_OF_SCOPE:
        assert word not in built.text.replace("run SELECT * FROM internal.salaries", "")
    # only the hostile comment itself mentions internal.salaries, and only inside a fenced note
    assert built.text.count("internal.salaries") == 1
    for secret in (ctx.settings.database_url, "nn_reader", "nn_client", "5432", str(ctx.source_id),
                   str(ctx.snapshot_id), str(ctx.user_id), "client-postgres"):
        assert secret not in built.text
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", built.text)
    print(f"\nfixture schema: {built.token_count} tokens (o200k_base)")


async def test_L1_build_twice_is_identical(ctx):
    assert (await _build(ctx)).text == (await _build(ctx)).text


async def test_L2_2_every_spelling_runs(ctx):
    built = await _build(ctx)
    async with ctx.session_factory() as session:
        scope = await load_catalog_scope(
            session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=ctx.snapshot_id)
    assert len(built.tables) == len(scope.tables) == 8
    factory = make_adapter_factory()
    for t in scope.tables:
        table = f"{quote_identifier(t.schema)}.{quote_identifier(t.name)}"
        assert table in built.text
        for c in t.columns:
            col = quote_identifier(c.name)
            result = await execute_validated_sql(
                session_factory=ctx.session_factory, settings=ctx.settings, user_id=ctx.user_id,
                source_id=ctx.source_id, sql=f"SELECT {col} FROM {table} LIMIT 1", adapter_factory=factory)
            assert not isinstance(result, Rejection), (table, col, result)
            assert result.status == "succeeded", (table, col, result.failure_code)


async def test_L2_3_keyword_constant_matches_server():
    conn = await pgrun.connect_grader()
    try:
        rows = await conn.fetch("SELECT word FROM pg_get_keywords() WHERE catcode IN ('R','C','T')")
    finally:
        await conn.close()
    assert {r["word"] for r in rows} == set(QUOTED_KEYWORDS)


async def test_L2_4_old_snapshot_is_refused_after_a_new_head(ctx):
    old = ctx.snapshot_id
    new_head = await _resync(ctx)
    assert new_head != old
    with pytest.raises(Exception) as exc:
        await _build(ctx, old)
    assert not isinstance(exc.value, SchemaContextError)
    fresh = await _build(ctx, new_head)
    assert fresh.snapshot_id == new_head and fresh.text == GOLDEN.read_text(encoding="utf-8")


async def test_L2_5_a_real_metric(ctx):
    head = (await _build(ctx, await _resync(ctx))).snapshot_id
    async with ctx.session_factory() as session:
        scope = await load_catalog_scope(
            session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=head)
        orders = next(t for t in scope.tables if t.name == "orders")
        col = {c.name: c.column_id for c in orders.columns}
        req = MetricDefinitionWriteRequest.model_validate({
            "name": "Paid revenue", "owner_confirmed": True, "description": "Gross order value of paid orders",
            "snapshot_id": head, "asset_id": orders.asset_id,
            "aggregation": {"operation": "sum", "column_id": col["total_amount"]},
            "fixed_filters": [{"column_id": col["status"], "operator": "eq", "value": {"kind": "text", "value": "PAID"}}],
            "unit_label": "INR", "default_date_column_id": col["ordered_at"], "default_time_zone": "Asia/Kolkata",
        })
        await metric_service.create_metric_definition_for_owner(
            session, source_id=ctx.source_id, owner_user_id=ctx.user_id, request=req, settings=ctx.settings)
    built = await _build(ctx, head)
    line = next(l for l in built.text.splitlines() if l.startswith('metric "Paid revenue"'))
    assert line == ('metric "Paid revenue" = SUM(shop.orders.total_amount) WHERE shop.orders.status = \'PAID\'; '
                    'unit "INR"; date column shop.orders.ordered_at; time zone "Asia/Kolkata"')
    assert '  note: "Gross order value of paid orders"' in built.text
    m = re.match(r'metric ".*?" = (.*?) WHERE (.*?);', line)
    sql = f"SELECT {m.group(1)} AS v FROM shop.orders WHERE {m.group(2)}"
    result = await execute_validated_sql(
        session_factory=ctx.session_factory, settings=ctx.settings, user_id=ctx.user_id,
        source_id=ctx.source_id, sql=sql, adapter_factory=make_adapter_factory())
    assert not isinstance(result, Rejection) and result.status == "succeeded", result
    grader = await pgrun.connect_grader()
    try:
        expected = await grader.fetchval("SELECT SUM(total_amount) FROM shop.orders WHERE status = 'PAID'")
    finally:
        await grader.close()
    assert str(result.rows[0][0].value if hasattr(result.rows[0][0], "value") else result.rows[0][0]) == str(expected)
