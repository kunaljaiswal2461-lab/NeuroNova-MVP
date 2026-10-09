"""SPEC-05 L2: validated SQL through the real adapter against the Part 0 fixture (run in the runner container)."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import sqlglot
from sqlglot import exp
from sqlalchemy import text

from app.connectors.connection_policy import PostgresConnectionPolicy
from app.connectors.postgres_adapter import PostgresOnboardingAdapter
from app.connectors.postgres_query_executor import PostgresExecutionCancellation, PostgresQueryExecutionAdapter
from app.db.models.catalog import CatalogSyncRun, CatalogSyncRunStatus
from app.services import catalog_service
from app.services.postgres_query_compiler import ExpectedPostgresOutputColumn
from app.services.postgres_query_executor import PostgresExecutionLimiter, _serialize_value
from app.services.sql_execution import SqlExecutionResult, execute_validated_sql
from app.services.sql_guard import Rejection, ValidatedQuery, load_catalog_scope, validate_sql
from app.workers.catalog_discovery_worker import CatalogDiscoveryWorker
from evals import pgrun
from evals.bootstrap import bootstrap, test_policy
from tests.sql_guard.conftest import ROW_CAP, accept_corpora, all_accept_sqls

pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio(loop_scope="module")]

ACCEPT = all_accept_sqls()
STATS: dict[str, Any] = {"max_cost": 0.0, "max_cost_id": None, "slowest_ms": 0.0, "slowest_id": None,
                         "gate_rejected": [], "dup_label_queries": 0, "dup_label_mismatch": [], "compared": 0,
                         "excluded": []}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ctx():
    c = await bootstrap()
    yield c
    await c.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def grader_conn():
    conn = await pgrun.connect_grader()
    yield conn
    await conn.close()


class _RecordingStatement:
    def __init__(self, statement, log):
        self._statement, self._log = statement, log

    def cursor(self, *args, **kwargs):
        self._log.append("CURSOR")
        return self._statement.cursor(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._statement, name)


class _RecordingConnection:
    """Records every statement the adapter sends, so a test can prove a cursor was (not) opened."""

    def __init__(self, connection, log):
        self._connection, self._log = connection, log

    async def execute(self, sql, *a, **k):
        self._log.append(sql)
        return await self._connection.execute(sql, *a, **k)

    async def fetchval(self, sql, *a, **k):
        self._log.append(sql)
        return await self._connection.fetchval(sql, *a, **k)

    async def fetchrow(self, sql, *a, **k):
        self._log.append(sql)
        return await self._connection.fetchrow(sql, *a, **k)

    async def fetch(self, sql, *a, **k):
        self._log.append(sql)
        return await self._connection.fetch(sql, *a, **k)

    async def prepare(self, sql, *a, **k):
        self._log.append(f"PREPARE {sql}")
        return _RecordingStatement(await self._connection.prepare(sql, *a, **k), self._log)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def make_adapter_factory(log: list[str] | None = None, *, in_flight: dict | None = None):
    async def connect(**kwargs):
        real = await asyncpg.connect(**kwargs)
        return _RecordingConnection(real, log if log is not None else [])

    class Adapter(PostgresQueryExecutionAdapter):
        async def execute_sql(self, **kwargs):
            if in_flight is None:
                return await super().execute_sql(**kwargs)
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            try:
                return await super().execute_sql(**kwargs)
            finally:
                in_flight["now"] -= 1

    return lambda settings: Adapter(settings=settings, policy=test_policy(), connect=connect)


async def run_sql(c, sql, *, settings=None, **kwargs):
    kwargs.setdefault("adapter_factory", make_adapter_factory())
    return await execute_validated_sql(
        session_factory=c.session_factory, settings=settings or c.settings, user_id=c.user_id,
        source_id=c.source_id, sql=sql, **kwargs,
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_scope(ctx):
    async with ctx.session_factory() as session:
        return await load_catalog_scope(
            session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=ctx.snapshot_id
        )


def _has_top_level_order(sql: str) -> bool:
    return sqlglot.parse_one(sql, read="postgres").args.get("order") is not None


async def _grader_rows(conn, sql: str):
    async with conn.transaction(readonly=True):
        stmt = await conn.prepare(sql)
        attrs = stmt.get_attributes()
        rows = [tuple(r) for r in await stmt.fetch()]
    return attrs, rows


# ── 1. corpus ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cid,sql", ACCEPT, ids=[c for c, _ in ACCEPT])
async def test_L2_1_corpus_values_equal_a_direct_grader_run(cid, sql, ctx, live_scope, grader_conn):
    validated = validate_sql(sql, live_scope, row_cap=ROW_CAP)
    assert isinstance(validated, ValidatedQuery), validated
    result = await run_sql(ctx, sql)
    assert isinstance(result, SqlExecutionResult)
    if not result.is_ok:
        STATS["gate_rejected"].append((cid, result.failure_code))
    assert result.is_ok, (cid, result.failure_code, result.message)
    assert result.sql == validated.sql                                         # I2 (also the plan's source)

    attrs, grows = await _grader_rows(grader_conn, validated.sql)
    expected_cols = [ExpectedPostgresOutputColumn(alias=a.name, logical_type=a.type.name, postgres_type=a.type.name,
                                                  source_column_id=None, source_column_name=None) for a in attrs]
    names = [a.name for a in attrs]
    if len(set(names)) < len(names):
        STATS["dup_label_queries"] += 1
    expected = [tuple(_serialize_value(v, c) for v, c in zip(row, expected_cols, strict=True)) for row in grows]

    cap = ROW_CAP
    got = list(result.rows)
    if len(expected) > cap:
        expected = expected[:cap]
        if not _has_top_level_order(validated.sql):
            STATS["excluded"].append((cid, "rows exceed the cap and the query has no ORDER BY"))
            assert len(got) == cap and result.safety_row_truncated
            return
    if not _has_top_level_order(validated.sql):
        expected, got = sorted(expected, key=repr), sorted(got, key=repr)
    if got != expected and len(set(names)) < len(names):
        STATS["dup_label_mismatch"].append(cid)
    assert got == expected                                                     # by position (I5)
    assert result.row_count <= cap and len(result.columns) == len(attrs)
    STATS["compared"] += 1
    if result.plan_cost is not None and result.plan_cost > STATS["max_cost"]:
        STATS["max_cost"], STATS["max_cost_id"] = result.plan_cost, cid
    if result.elapsed_ms > STATS["slowest_ms"]:
        STATS["slowest_ms"], STATS["slowest_id"] = result.elapsed_ms, cid


async def test_L2_1_gate_report_is_printed_last():
    print(f"\nSPEC-05 corpus: compared={STATS['compared']} max_plan_cost={STATS['max_cost']:.0f} "
          f"({STATS['max_cost_id']}) slowest_ms={STATS['slowest_ms']:.0f} ({STATS['slowest_id']}) "
          f"gate_rejected={len(STATS['gate_rejected'])} dup_label_queries={STATS['dup_label_queries']} "
          f"excluded={len(STATS['excluded'])}")
    assert STATS["gate_rejected"] == []
    assert STATS["dup_label_mismatch"] == []


# ── 2. duplicates ────────────────────────────────────────────────────────────

async def test_L2_2_duplicate_names_are_read_by_position(ctx, grader_conn):
    result = await run_sql(ctx, "SELECT count(*), count(*) FILTER (WHERE status = 'PAID') FROM shop.orders")
    assert result.is_ok, result
    assert [c.label for c in result.columns] == ["count", "count_2"]
    assert [v.value for v in result.rows[0]] == ["60000", "42020"]
    # asyncpg by name would give the last duplicate for both (the F4 hazard this design avoids):
    async with grader_conn.transaction(readonly=True):
        rec = await grader_conn.fetchrow("SELECT count(*), count(*) FILTER (WHERE status = 'PAID') FROM shop.orders")
    assert [rec["count"], rec[0]] == [42020, 60000]


async def test_L2_2_blank_and_unnamed_columns_get_col_n(ctx):
    result = await run_sql(ctx, "SELECT 1, 2, id FROM shop.regions ORDER BY id LIMIT 1")
    assert result.is_ok, result
    assert [c.label for c in result.columns] == ["col_1", "col_2", "id"]


# ── 2b. table-less SQL is refused (SPEC-05 decision) ──────────────────────────

@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT count(*) FROM generate_series(1, 1000000000)",
                                 "SELECT age(timestamp '2026-06-01', timestamp '2025-01-15')"])
async def test_L2_2b_table_less_sql_is_a_rejection_and_never_connects(sql, ctx):
    log: list[str] = []
    result = await run_sql(ctx, sql, adapter_factory=make_adapter_factory(log))
    assert isinstance(result, Rejection) and result.code.value == "no_table_reference"
    assert log == []


# ── 3. types ─────────────────────────────────────────────────────────────────

TYPE_CASES = {
    "age": ("SELECT age(timestamp '2026-06-01', timestamp '2025-01-15') AS a FROM shop.regions LIMIT 1", "extract(epoch"),
    "timestamp-minus": ("SELECT refunded_at - refunded_at AS d FROM shop.refunds LIMIT 3", "extract(epoch"),
    "whole-row": ("SELECT o FROM shop.orders o LIMIT 3", "select columns, not a whole row"),
    "row-ctor": ("SELECT ROW(id, name) FROM shop.regions LIMIT 3", "select columns, not a whole row"),
    "money": ("SELECT total_amount::money FROM shop.orders LIMIT 3", "cast to numeric"),
    "array": ("SELECT array_agg(id) FROM shop.regions", "cast"),
}


@pytest.mark.parametrize("name", sorted(TYPE_CASES))
async def test_L2_3_unsupported_output_types_never_open_a_cursor(name, ctx, live_scope):
    sql, hint = TYPE_CASES[name]
    validated = validate_sql(sql, live_scope, row_cap=ROW_CAP)
    if isinstance(validated, Rejection):
        # The validator already refuses it (e.g. a cast target outside its allowlist): even stronger.
        print(f"\n{name}: refused earlier by the validator: {validated.code.value}")
        return
    log: list[str] = []
    result = await run_sql(ctx, sql, adapter_factory=make_adapter_factory(log))
    assert result.status == "failed" and result.failure_code == "unsupported_output_type", result
    assert hint in result.message and result.rows == ()
    assert "CURSOR" not in log and not any(s.startswith("EXPLAIN") for s in log)


async def test_L2_3_a_supported_query_does_open_the_cursor(ctx):
    log: list[str] = []
    result = await run_sql(ctx, "SELECT id FROM shop.regions", adapter_factory=make_adapter_factory(log))
    assert result.is_ok and "CURSOR" in log
    assert [i for i, s in enumerate(log) if s.startswith("EXPLAIN")][0] < log.index("CURSOR")


# ── 4. cost ──────────────────────────────────────────────────────────────────

PATHOLOGICAL = {
    "cross_join_pv_orders": "SELECT count(*) FROM shop.page_views pv CROSS JOIN shop.orders o",
    "self_join_pv_path": "SELECT count(*) FROM shop.page_views a JOIN shop.page_views b ON a.path = b.path",
    "cross_join_orders_items": "SELECT count(*) FROM shop.orders o CROSS JOIN shop.order_items i",
    "generate_series_1e9": "SELECT count(*) FROM shop.regions r, generate_series(1, 1000000000) g",
}
EXPENSIVE_BUT_ALLOWED = {
    "pv_full_sort": "SELECT id, path FROM shop.page_views ORDER BY path, viewed_at, id",
    "pv_distinct_path": "SELECT DISTINCT customer_id, path FROM shop.page_views",
    "generate_series_1e7": "SELECT count(*) FROM shop.regions r, generate_series(1, 1000000) g",
}


@pytest.mark.parametrize("name", sorted(PATHOLOGICAL))
async def test_L2_4_pathological_queries_are_refused_fast(name, ctx):
    started = time.monotonic()
    result = await run_sql(ctx, PATHOLOGICAL[name])
    wall = time.monotonic() - started
    assert result.status == "failed" and result.failure_code == "query_too_expensive", result
    assert wall < 1.0, wall
    assert result.rows == ()


@pytest.mark.parametrize("name", sorted(EXPENSIVE_BUT_ALLOWED))
async def test_L2_4_expensive_but_reasonable_queries_still_run(name, ctx):
    result = await run_sql(ctx, EXPENSIVE_BUT_ALLOWED[name])
    assert result.is_ok, result
    assert result.plan_cost < ctx.settings.postgres_sql_max_plan_cost


async def test_L2_4_ceiling_of_1000_rejects_nat_08(ctx):
    nat08 = dict(accept_corpora()["natural"])["nat-08"]
    assert (await run_sql(ctx, nat08)).is_ok                                    # default ceiling: passes
    tight = ctx.settings.model_copy(update={"postgres_sql_max_plan_cost": 1_000.0})
    result = await run_sql(ctx, nat08, settings=tight)
    assert result.failure_code == "query_too_expensive"


# ── 5. timeout backstop ──────────────────────────────────────────────────────

async def test_L2_5_cost_is_not_time_the_statement_timeout_is_the_backstop(ctx):
    sql = "SELECT count(*) FROM shop.regions r, generate_series(1, 1000000000) g"
    raised = ctx.settings.model_copy(update={"postgres_sql_max_plan_cost": 1e10})
    started = time.monotonic()
    result = await run_sql(ctx, sql, settings=raised)
    assert result.status == "failed" and result.failure_code == "execution_timeout", result
    assert time.monotonic() - started < 12


# ── 6. drift / privilege (one scratch table each, owned by nn_drift) ─────────

@pytest_asyncio.fixture(loop_scope="module")
async def drift():
    """A scratch table in schema `drift`, a throwaway source selecting only it, and a way to change it."""
    name = f"probe_{uuid.uuid4().hex[:8]}"
    owner = await pgrun.connect_drift()
    await owner.execute(f"CREATE TABLE drift.{name} (id int8 PRIMARY KEY, v text)")
    await owner.execute(f"INSERT INTO drift.{name} VALUES (1, 'a'), (2, 'b')")
    c = await bootstrap(selected_tables=[("drift", name)])
    try:
        yield c, owner, name
    finally:
        await c.dispose()
        for stmt in (f"DROP VIEW IF EXISTS drift.{name}", f"DROP TABLE IF EXISTS drift.{name}",
                     f"DROP TABLE IF EXISTS drift.{name}_base"):
            try:
                await owner.execute(stmt)
            except asyncpg.PostgresError:
                pass
        await owner.close()


def drift_sql(name: str) -> str:
    return f"SELECT id, v FROM drift.{name}"


async def test_L2_6_baseline_runs(drift):
    c, _owner, name = drift
    result = await run_sql(c, drift_sql(name))
    assert result.is_ok, result
    assert [[v.value for v in r] for r in result.rows] == [["1", "a"], ["2", "b"]]


DRIFTS = {
    "drop-referenced-column": ("ALTER TABLE drift.{t} DROP COLUMN v", "schema_changed"),
    "revoke-select": ("REVOKE SELECT ON drift.{t} FROM nn_reader", "access_changed"),
    "enable-rls": ("ALTER TABLE drift.{t} ENABLE ROW LEVEL SECURITY", "restricted_role_required"),
    "swap-for-view": (
        "ALTER TABLE drift.{t} RENAME TO {t}_base; CREATE VIEW drift.{t} AS SELECT * FROM drift.{t}_base; "
        "GRANT SELECT ON drift.{t} TO nn_reader", "schema_changed"),
    "change-owner": (
        # ALTER ... OWNER TO needs CREATE on the schema for the new owner; hand it out only for the move.
        "GRANT CREATE ON SCHEMA drift TO nn_reader; ALTER TABLE drift.{t} OWNER TO nn_reader; "
        "REVOKE CREATE ON SCHEMA drift FROM nn_reader", "restricted_role_required"),
    "rename-column": ("ALTER TABLE drift.{t} RENAME COLUMN v TO w", "schema_changed"),
    "change-column-type": ("ALTER TABLE drift.{t} ALTER COLUMN v TYPE varchar(10)", "schema_changed"),
}


@pytest.mark.parametrize("case", sorted(DRIFTS))
async def test_L2_6_drift_gives_the_mapped_code_and_no_rows(case, drift):
    c, owner, name = drift
    assert (await run_sql(c, drift_sql(name))).is_ok
    ddl, code = DRIFTS[case]
    for stmt in ddl.format(t=name).split(";"):
        if stmt.strip():
            await owner.execute(stmt)
    result = await run_sql(c, drift_sql(name))
    assert result.status == "failed" and result.failure_code == code, result
    assert result.rows == ()


# ── 7. head change ───────────────────────────────────────────────────────────

async def _resync(ctx) -> uuid.UUID:
    policy = test_policy()
    worker = CatalogDiscoveryWorker(
        settings=ctx.settings, session_factory=ctx.session_factory,
        adapter_factory=lambda s: PostgresOnboardingAdapter(settings=s, policy=policy),
        worker_id=f"spec05-{uuid.uuid4().hex[:6]}",
    )
    async with ctx.session_factory() as session:
        run = await catalog_service.request_catalog_sync_for_owner(session, source_id=ctx.source_id, owner_user_id=ctx.user_id)
        run_id = run.id
    for _ in range(120):
        await worker.run_once()
        async with ctx.session_factory() as session:
            status = (await session.get(CatalogSyncRun, run_id)).status
        if status in (CatalogSyncRunStatus.SUCCEEDED, CatalogSyncRunStatus.FAILED, CatalogSyncRunStatus.CANCELLED):
            break
        await asyncio.sleep(0.1)
    assert status is CatalogSyncRunStatus.SUCCEEDED, status
    async with ctx.session_factory() as session:
        return (await catalog_service.get_current_catalog_snapshot_for_owner(
            session, source_id=ctx.source_id, owner_user_id=ctx.user_id)).id


async def test_L2_7_a_new_head_is_picked_up_by_the_next_execution(ctx):
    sql = "SELECT count(*) FROM shop.regions"
    first = await run_sql(ctx, sql)
    new_head = await _resync(ctx)
    second = await run_sql(ctx, sql)
    assert first.is_ok and second.is_ok
    assert second.snapshot_id == new_head and first.snapshot_id != new_head


# ── 8. cancellation ──────────────────────────────────────────────────────────

async def test_L2_8_cancel_mid_query_terminates_the_connection_and_finalizes_the_run(ctx):
    cancellation = PostgresExecutionCancellation()
    slow = "SELECT count(*) FROM shop.regions r, generate_series(1, 50000000) g"
    raised = ctx.settings.model_copy(update={"postgres_sql_max_plan_cost": 1e10})
    task = asyncio.create_task(run_sql(ctx, slow, cancellation=cancellation, settings=raised))
    await asyncio.sleep(1.0)
    await cancellation.cancel()
    result = await asyncio.wait_for(task, 10)
    assert result.status == "cancelled" and result.failure_code == "execution_cancelled", result
    async with ctx.session_factory() as session:
        row = (await session.execute(text(
            "SELECT status, query_kind FROM postgres_query_runs WHERE id = :i"), {"i": result.run_id})).one()
    assert (row.status, row.query_kind) == ("CANCELLED", "sql")
    conn = await pgrun.connect_grader()
    try:
        for _ in range(40):
            if not await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE usename = 'nn_reader' "
                                       "AND application_name = 'neuronova-postgres-query'"):
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("the client backend is still alive after cancellation")
    finally:
        await conn.close()


# ── 9. concurrency ───────────────────────────────────────────────────────────

BUSY = "SELECT count(*) FROM shop.regions r, generate_series(1, 1000000) g"


@pytest.mark.parametrize(
    "limits,expected_max", [((2, 1, 1), 1), ((2, 5, 5), 2), ((10, 5, 5), 5)],
)
async def test_L2_9_limiter_caps_hold_under_ten_parallel_calls(ctx, limits, expected_max):
    in_flight = {"now": 0, "max": 0}
    limiter = PostgresExecutionLimiter(global_limit=limits[0], per_user_limit=limits[1], per_source_limit=limits[2])
    factory = make_adapter_factory(in_flight=in_flight)
    results = await asyncio.gather(*[run_sql(ctx, BUSY, limiter=limiter, adapter_factory=factory) for _ in range(10)])
    assert all(r.is_ok for r in results), [r.failure_code for r in results]
    assert in_flight["max"] == expected_max, in_flight


# ── 10. catalog-sync stress ──────────────────────────────────────────────────

async def test_L2_10_sync_then_execute_fifty_times_without_a_failure(ctx):
    failures: list[str] = []
    for i in range(50):
        await _resync(ctx)
        result = await run_sql(ctx, "SELECT count(*) FROM shop.orders")
        if not result.is_ok:
            failures.append(f"{i}: {result.failure_code}")
    assert failures == []


# ── 11. I7 scan ──────────────────────────────────────────────────────────────

def _literals(sql: str) -> set[str]:
    out: set[str] = set()
    for lit in sqlglot.parse_one(sql, read="postgres").find_all(exp.Literal):
        if lit.is_string and len(lit.name) >= 4:
            out.add(lit.name)
    return out


async def test_L2_11_no_corpus_literal_or_sql_text_in_the_app_db(ctx):
    # A few queries with distinctive literals, run just now, alongside everything the corpus run wrote.
    secrets = [f"needle-{uuid.uuid4().hex[:10]}" for _ in range(3)]
    for s in secrets:
        r = await run_sql(ctx, f"SELECT id FROM shop.regions WHERE name = '{s}'")
        assert r.is_ok, r
    literals = {l for _, sql in ACCEPT for l in _literals(sql)} | set(secrets)
    async with ctx.session_factory() as session:
        columns = (await session.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND data_type IN ('character varying', 'text', 'json', 'jsonb')"))).all()
        runs = (await session.execute(text("SELECT count(*) FROM postgres_query_runs WHERE query_kind = 'sql'"))).scalar_one()
        assert runs >= len(secrets)
        hits: list[str] = []
        for table, column in columns:
            values = (await session.execute(text(f'SELECT "{column}"::text FROM "{table}" WHERE "{column}" IS NOT NULL'))).scalars().all()
            blob = "\n".join(values)
            for lit in literals:
                # Catalog tables legitimately hold identifier names; only run bookkeeping can leak a value.
                if table.startswith("postgres_query_run") and lit in blob:
                    hits.append(f"{table}.{column}: {lit!r}")
            if table.startswith("postgres_query_run") and "select " in blob.lower():
                hits.append(f"{table}.{column}: SQL text")
        assert hits == []
        for s in secrets:                                 # and nowhere at all, in any table
            for table, column in columns:
                assert (await session.execute(text(
                    f'SELECT count(*) FROM "{table}" WHERE "{column}"::text LIKE :p'), {"p": f"%{s}%"})).scalar_one() == 0


# ── 12. the cancellation fix applies to the compiled path too ─────────────────

async def _backend_alive() -> int:
    conn = await pgrun.connect_grader()
    try:
        return await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE usename = 'nn_reader' "
                                   "AND application_name = 'neuronova-postgres-query'")
    finally:
        await conn.close()


async def test_L2_12_compiled_path_cancel_stops_the_backend_promptly(ctx):
    """A heavy compiled plan (group 2M page_views rows by customer, path and time: ~2.3 s alone), cancelled at 0.5 s."""
    from sqlalchemy import select
    from app.db.models.catalog import CatalogAsset, CatalogColumn
    from app.services.postgres_query_executor import execute_postgres_query
    from app.services.postgres_query_plan import PostgresQueryPlan

    async with ctx.session_factory() as session:
        head = (await catalog_service.get_current_catalog_snapshot_for_owner(
            session, source_id=ctx.source_id, owner_user_id=ctx.user_id)).id     # earlier tests re-synced
        asset = (await session.execute(select(CatalogAsset).where(
            CatalogAsset.source_id == ctx.source_id, CatalogAsset.snapshot_id == head,
            CatalogAsset.table_name == "page_views"))).scalar_one()
        cols = {c.column_name: c.id for c in (await session.execute(select(CatalogColumn).where(
            CatalogColumn.asset_id == asset.id))).scalars()}
    plan = PostgresQueryPlan.model_validate({
        "asset_id": asset.id,
        "group_by": [{"column_id": cols["customer_id"]}, {"column_id": cols["path"]}, {"column_id": cols["viewed_at"]}],
        "aggregations": [{"operation": "count_all"}],
        "sort": {"output_kind": "aggregation", "output_index": 0, "direction": "desc"},
        "limit": 100,
    })
    cancellation = PostgresExecutionCancellation()
    started = time.monotonic()
    task = asyncio.create_task(execute_postgres_query(
        ctx.session_factory, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=head,
        plan=plan, settings=ctx.settings, cancellation=cancellation,
        adapter_factory=lambda s: PostgresQueryExecutionAdapter(settings=s, policy=test_policy()),
    ))
    await asyncio.sleep(0.5)
    await cancellation.cancel()
    result = await asyncio.wait_for(task, 10)
    assert result.status == "cancelled", (result.status, result.failure_code)
    for _ in range(10):                       # 1 s: the query alone needs ~1.8 s more, so only the fix passes
        if not await _backend_alive():
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("compiled path: backend still running after cancellation")
    assert time.monotonic() - started < 2.2


# ── 13. lpad/rpad cannot build a huge value; non-finite values are strings ───

@pytest.mark.parametrize("sql", [
    "SELECT lpad('a', 200000000) FROM shop.regions LIMIT 1",
    "SELECT rpad(name, 200000000, 'xy') FROM shop.regions LIMIT 1",
    "SELECT lpad(name, id * 100000000) FROM shop.regions LIMIT 1",
])
async def test_L2_13_huge_pad_is_rejected_before_any_connection_and_memory_stays_flat(sql, ctx):
    import resource

    log: list[str] = []
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss          # KiB, peak so far
    result = await run_sql(ctx, sql, adapter_factory=make_adapter_factory(log))
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert isinstance(result, Rejection) and result.code.value == "function_not_allowed"
    assert "10,000" in result.message
    assert log == []                                        # never connected
    assert after - before < 50 * 1024                       # nowhere near a 200 MB value


async def test_L2_13_a_pad_at_the_limit_still_runs_and_is_exactly_that_long(ctx):
    result = await run_sql(ctx, "SELECT rpad(name, 10000, 'x') AS s FROM shop.regions ORDER BY id LIMIT 1")
    assert result.is_ok, result
    assert len(result.rows[0][0].value) == 10000


NON_FINITE = {
    "numeric-nan": ("SELECT 'NaN'::numeric AS v FROM shop.regions LIMIT 1", "non_finite_number", "NaN"),
    "numeric-inf": ("SELECT 'Infinity'::numeric AS v FROM shop.regions LIMIT 1", "non_finite_number", "Infinity"),
    "numeric-neginf": ("SELECT '-Infinity'::numeric AS v FROM shop.regions LIMIT 1", "non_finite_number", "-Infinity"),
    "float-nan": ("SELECT 'NaN'::double precision AS v FROM shop.regions LIMIT 1", "non_finite_number", "NaN"),
    "date-inf": ("SELECT 'infinity'::date AS v FROM shop.regions LIMIT 1", "date", "infinity"),
    "date-neginf": ("SELECT '-infinity'::date AS v FROM shop.regions LIMIT 1", "date", "-infinity"),
    "timestamp-inf": ("SELECT 'infinity'::timestamp AS v FROM shop.regions LIMIT 1", "timestamp", "infinity"),
    "timestamp-neginf": ("SELECT '-infinity'::timestamp AS v FROM shop.regions LIMIT 1", "timestamp", "-infinity"),
    "timestamptz-inf": ("SELECT 'infinity'::timestamptz AS v FROM shop.regions LIMIT 1", "timestamptz", "infinity"),
    "timestamptz-neginf": ("SELECT '-infinity'::timestamptz AS v FROM shop.regions LIMIT 1", "timestamptz", "-infinity"),
}


@pytest.mark.parametrize("name", sorted(NON_FINITE))
async def test_L2_13_non_finite_values_are_strings(name, ctx):
    sql, kind, value = NON_FINITE[name]
    result = await run_sql(ctx, sql)
    assert result.is_ok, result
    cell = result.rows[0][0]
    assert (cell.kind, cell.value) == (kind, value)


# ── 14. per-row size guard: bombs built from allowed pieces never reach the app ───

ROW_BOMBS = {
    # 10,000-char pad, every 'x' replaced by another 10,000-char pad: a 100 MB value (lpad cap alone is bypassed).
    "replace-of-pads": "SELECT replace(lpad(name, 10000, 'x'), 'x', lpad('y', 10000, 'z')) AS s FROM shop.regions LIMIT 1",
    # A 10,000,000-char blank-padded char value from a plain cast.
    "char-cast": "SELECT CAST('a' AS CHAR(10000000)) AS s FROM shop.regions LIMIT 1",
}


ROW_BOMBS.update({
    # An output column named q must not capture the guard's row reference (bare q::text would resolve to it).
    "column-named-q": "SELECT 'x' AS q, replace(lpad(name, 10000, 'x'), 'x', lpad('y', 10000, 'z')) AS big FROM shop.regions LIMIT 1",
    "column-named-q-last": "SELECT replace(lpad(name, 10000, 'x'), 'x', lpad('y', 10000, 'z')) AS big, 'x' AS q FROM shop.regions LIMIT 1",
    "only-q": "SELECT replace(lpad(name, 10000, 'x'), 'x', lpad('y', 10000, 'z')) AS q FROM shop.regions LIMIT 1",
})


@pytest.mark.parametrize("name", sorted(ROW_BOMBS))
async def test_L2_14_oversized_rows_are_refused_by_the_server_before_any_row_reaches_the_app(name, ctx):
    import resource

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = await run_sql(ctx, ROW_BOMBS[name])
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert not isinstance(result, Rejection), result       # the validator allows it; the row guard is what refuses
    assert result.status == "failed" and result.failure_code == "result_row_too_large", result
    assert result.rows == () and result.row_count == 0
    assert result.message == "A result row is too large to return. Select fewer or shorter columns."
    assert "nn_row_too_large" not in (result.message or "")
    assert after - before < 50 * 1024                       # a 100 MB value never crossed the wire


async def test_L2_14_the_limit_is_a_setting_and_a_row_just_under_it_runs(ctx):
    small = ctx.settings.model_copy(update={"postgres_sql_max_row_bytes": 1_024})
    ok = await run_sql(ctx, "SELECT rpad(name, 500, 'x') AS s FROM shop.regions ORDER BY id LIMIT 1", settings=small)
    assert ok.is_ok, ok
    assert len(ok.rows[0][0].value) == 500
    big = await run_sql(ctx, "SELECT rpad(name, 2000, 'x') AS s FROM shop.regions ORDER BY id LIMIT 1", settings=small)
    assert big.failure_code == "result_row_too_large"


async def test_L2_14_the_guard_is_not_folded_away_so_ordinary_queries_still_run(ctx):
    result = await run_sql(ctx, "SELECT id, name FROM shop.regions ORDER BY id LIMIT 3")
    assert result.is_ok, result
    assert result.row_count == 3


async def test_L2_14_duplicate_labels_and_order_by_survive_the_wrapper(ctx, grader_conn):
    result = await run_sql(ctx, "SELECT id, id, name FROM shop.regions ORDER BY id DESC LIMIT 4")
    assert result.is_ok, result
    assert [c.label for c in result.columns] == ["id", "id_2", "name"]
    async with grader_conn.transaction(readonly=True):
        expected = await grader_conn.fetch("SELECT id, id, name FROM shop.regions ORDER BY id DESC LIMIT 4")
    assert [[c.value for c in row] for row in result.rows] == [[str(r[0]), str(r[1]), r[2]] for r in expected]
    assert [row[0].value for row in result.rows] == sorted((row[0].value for row in result.rows), key=int, reverse=True)
    # And a computed ORDER BY on an aggregate keeps its order too.
    agg = await run_sql(ctx, "SELECT status, count(*) AS n FROM shop.orders GROUP BY status ORDER BY n DESC")
    assert agg.is_ok, agg
    counts = [int(row[1].value) for row in agg.rows]
    assert counts == sorted(counts, reverse=True)


# ── 15. dates: infinity and 9999-12-31 stay distinct (text codecs), on both paths ───

async def test_L2_15_infinity_and_the_9999_sentinel_are_different_values(ctx):
    result = await run_sql(
        ctx,
        "SELECT DATE '9999-12-31' AS d_max, 'infinity'::date AS d_inf, '-infinity'::date AS d_ninf, "
        "TIMESTAMP '9999-12-31 23:59:59' AS t_max, 'infinity'::timestamp AS t_inf, "
        "TIMESTAMPTZ '9999-12-31 23:59:59+00' AS z_max, 'infinity'::timestamptz AS z_inf, "
        "'-infinity'::timestamptz AS z_ninf FROM shop.regions LIMIT 1",
    )
    assert result.is_ok, result
    assert [c.value for c in result.rows[0]] == [
        "9999-12-31", "infinity", "-infinity",
        "9999-12-31T23:59:59", "infinity",
        "9999-12-31T23:59:59Z", "infinity", "-infinity",
    ]


async def test_L2_15_ordinary_values_keep_their_spelling_including_fractions_and_offsets(ctx):
    result = await run_sql(
        ctx,
        "SELECT TIMESTAMP '2026-01-02 03:04:05.5' AS a, TIMESTAMPTZ '2026-01-02 08:34:05.25+05:30' AS b, "
        "DATE '2026-01-02' AS c, TIMESTAMP '2026-01-02 03:04:05' AS d FROM shop.regions LIMIT 1",
    )
    assert result.is_ok, result
    assert [c.value for c in result.rows[0]] == [
        "2026-01-02T03:04:05.500000", "2026-01-02T03:04:05.250000Z", "2026-01-02", "2026-01-02T03:04:05",
    ]


async def test_L2_15_the_compiled_path_gets_the_same_text_codecs(ctx):
    from sqlalchemy import select
    from app.db.models.catalog import CatalogAsset, CatalogColumn
    from app.services.postgres_query_executor import execute_postgres_query
    from app.services.postgres_query_plan import PostgresQueryPlan

    async with ctx.session_factory() as session:
        head = (await catalog_service.get_current_catalog_snapshot_for_owner(
            session, source_id=ctx.source_id, owner_user_id=ctx.user_id)).id
        asset = (await session.execute(select(CatalogAsset).where(
            CatalogAsset.source_id == ctx.source_id, CatalogAsset.snapshot_id == head,
            CatalogAsset.table_name == "orders"))).scalar_one()
        cols = {c.column_name: c.id for c in (await session.execute(select(CatalogColumn).where(
            CatalogColumn.asset_id == asset.id))).scalars()}
    plan = PostgresQueryPlan.model_validate({
        "asset_id": asset.id,
        "group_by": [{"column_id": cols["delivered_on"]}, {"column_id": cols["ordered_at"]}],
        "aggregations": [{"operation": "count_all"}],
        "sort": {"output_kind": "aggregation", "output_index": 0, "direction": "desc"},
        "limit": 5,
    })
    result = await execute_postgres_query(
        ctx.session_factory, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=head,
        plan=plan, settings=ctx.settings,
        adapter_factory=lambda s: PostgresQueryExecutionAdapter(settings=s, policy=test_policy()),
    )
    assert result.status == "succeeded", (result.status, result.failure_code)
    import re
    for row in result.rows:
        kinds = [cell.kind for cell in row]
        assert kinds[:2] == ["date", "timestamptz"] or row[0].kind == "null"
        if row[0].kind == "date":
            assert re.fullmatch(r"\d{4}-\d\d-\d\d", row[0].value)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", row[1].value)


async def test_L2_15_out_of_range_values_come_back_as_postgres_writes_them(ctx):
    result = await run_sql(
        ctx,
        "SELECT DATE '0044-03-15 BC' AS a, TIMESTAMP '0044-03-15 01:02:03 BC' AS b, "
        "TIMESTAMPTZ '12345-01-02 03:04:05+00' AS c FROM shop.regions LIMIT 1",
    )
    assert result.is_ok, result
    assert [c.value for c in result.rows[0]] == ["0044-03-15 BC", "0044-03-15 01:02:03 BC", "12345-01-02 03:04:05+00"]
