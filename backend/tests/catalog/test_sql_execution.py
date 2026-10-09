"""SPEC-05 L1: validated-SQL execution through the adapter and the service (SQLite app DB, fake client DB).

The client side is an asyncpg-shaped fake: it proves the order of checks, the gates and the bookkeeping, not
PostgreSQL behaviour. The live layer (tests/live/test_sql_execution_live.py) covers the real database.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.connectors.postgres_query_executor import (
    LiveRelationExpectation,
    LiveSqlExpectation,
    PostgresExecutionAdapterError,
    PostgresExecutionConfigurationError,
    PostgresExecutionCostExceededError,
    PostgresExecutionRowTooLargeError,
    PostgresExecutionUnsupportedOutputError,
    PostgresQueryExecutionAdapter,
    dedupe_output_labels,
)
from app.connectors.postgres_query_executor import _wrap_row_size_guard
from app.connectors.connection_policy import PostgresConnectionPolicy
from app.core.config import Settings
from app.db.base import Base
from app.db.models.postgres_query_run import (
    PostgresQueryRun,
    PostgresQueryRunAsset,
    PostgresQueryRunStatus,
)
from app.services.sql_execution import SqlExecutionResult, execute_validated_sql
from app.services.sql_guard import Rejection, SealedValidatedQuery, load_catalog_scope, seal, validate_sql
from tests.catalog.test_postgres_query_executor import (  # noqa: F401  (fixtures + fakes)
    _TABLES,
    _Connection,
    _attribute,
    _ExecutionCatalog,
    _live_column,
    _relation_row,
    _resolver,
    execution_catalog,
    execution_settings,
)

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import event


@pytest.fixture
async def execution_session_factory():
    """Shadows the Stage 4B fixture of the same name: this one also creates the SPEC-05 child table."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=[*_TABLES, PostgresQueryRunAsset.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


SECRET = "zzz-secret-literal-7731"
SQL = f"SELECT id, amount, label FROM sales.orders WHERE label = '{SECRET}'"


class _SqlConnection(_Connection):
    """Adds what the SQL path needs: EXPLAIN via fetchval, and a log of every statement sent."""

    def __init__(self, *, cost: float = 100.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cost = cost
        self.log: list[str] = []
        self.explained: list[str] = []

    async def execute(self, sql: str):
        self.log.append(sql)
        await super().execute(sql)

    async def prepare(self, sql: str):
        self.log.append(f"PREPARE {sql}")
        return await super().prepare(sql)

    async def fetchval(self, sql: str, *parameters: Any):
        self.log.append(sql)
        self.explained.append(sql)
        return json.dumps([{"Plan": {"Total Cost": self.cost}}])


def _connection(catalog: _ExecutionCatalog, *, attributes=None, records=None, **kwargs) -> _SqlConnection:
    attributes = attributes if attributes is not None else [
        _attribute("id", "int8"), _attribute("amount", "numeric"), _attribute("label", "text")
    ]
    return _SqlConnection(
        live_columns={n: _live_column(c) for n, c in catalog.columns.items()},
        attributes=attributes,
        records=records if records is not None else [],
        **kwargs,
    )


def _adapter(catalog: _ExecutionCatalog, connection: _SqlConnection):
    calls: list[dict[str, Any]] = []

    async def connect(**kwargs: Any):
        calls.append(kwargs)
        return connection

    adapter = PostgresQueryExecutionAdapter(
        settings=catalog.settings,
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )
    return adapter, calls


async def _run(catalog: _ExecutionCatalog, connection: _SqlConnection, sql: str = SQL, **kwargs):
    adapter, calls = _adapter(catalog, connection)
    result = await execute_validated_sql(
        session_factory=catalog.factory,
        settings=catalog.settings,
        user_id=kwargs.pop("user_id", catalog.owner_id),
        source_id=kwargs.pop("source_id", catalog.source_id),
        sql=sql,
        adapter_factory=lambda _s: adapter,
        **kwargs,
    )
    return result, calls


async def _sealed(catalog: _ExecutionCatalog, sql: str = SQL) -> SealedValidatedQuery:
    async with catalog.factory() as session:
        scope = await load_catalog_scope(
            session, owner_user_id=catalog.owner_id, source_id=catalog.source_id, snapshot_id=catalog.snapshot_id
        )
    row_cap = catalog.settings.postgres_execution_max_result_rows
    return seal(validate_sql(sql, scope, row_cap=row_cap), scope, row_cap=row_cap)


def _expectation(catalog: _ExecutionCatalog, *names: str) -> LiveSqlExpectation:
    from app.services.postgres_query_executor import _live_columns

    return LiveSqlExpectation(
        relations=(
            LiveRelationExpectation("sales", "orders", _live_columns({catalog.columns[n].id: catalog.columns[n] for n in names})),
        )
    )


async def _adapter_call(catalog, connection, sealed, *, expectation=None, consumer=None, **kwargs):
    adapter, calls = _adapter(catalog, connection)
    rows: list[tuple] = []

    def default_consumer(values):
        rows.append(values)
        return True

    remote = await adapter.execute_sql(
        host="client-postgres.stage4b.test", port=5432, database_name="analytics", username="u",
        password="p", custom_ca_pem=None, sealed=sealed,
        expectation=expectation or _expectation(catalog, "id", "amount", "label"),
        max_rows=catalog.settings.postgres_execution_max_result_rows,
        row_consumer=consumer or default_consumer, **kwargs,
    )
    return remote, rows, calls


# ── labels ───────────────────────────────────────────────────────────────────

def test_label_dedupe_and_blank_names():
    assert dedupe_output_labels(["count", "count"]) == ("count", "count_2")
    assert dedupe_output_labels(["count", "count", "count"]) == ("count", "count_2", "count_3")
    assert dedupe_output_labels(["?column?", "", "a", "?column?"]) == ("col_1", "col_2", "a", "col_4")
    assert dedupe_output_labels(["a", "a", "a_2"]) == ("a", "a_2", "a_2_2")        # never collides
    assert dedupe_output_labels([]) == ()


# ── I1: only a sealed query reaches the adapter ──────────────────────────────

async def test_adapter_refuses_anything_that_is_not_sealed_before_connecting(execution_catalog):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    forged = dataclasses.replace(sealed, sql="SELECT pg_sleep(100)")
    for bad in (forged, dataclasses.replace(sealed), "SELECT 1", None, SimpleNamespace(sql="SELECT 1")):
        connection = _connection(catalog)
        with pytest.raises(PostgresExecutionConfigurationError):
            await _adapter_call(catalog, connection, bad)
        assert connection.log == [] and connection.prepared_sql is None
    # And a connect was never even attempted:
    adapter, calls = _adapter(catalog, _connection(catalog))
    with pytest.raises(PostgresExecutionConfigurationError):
        await adapter.execute_sql(
            host="h", port=5432, database_name="d", username="u", password="p", custom_ca_pem=None,
            sealed=forged, expectation=_expectation(catalog, "id"), max_rows=5, row_consumer=lambda v: True,
        )
    assert calls == []


async def test_adapter_refuses_facts_that_do_not_describe_the_sealed_tables(execution_catalog):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    wrong = LiveSqlExpectation(relations=(LiveRelationExpectation("sales", "other", ()),))
    empty = LiveSqlExpectation(relations=())
    for expectation in (wrong, empty):
        connection = _connection(catalog)
        with pytest.raises(PostgresExecutionConfigurationError):
            await _adapter_call(catalog, connection, sealed, expectation=expectation)
        assert connection.log == []


# ── I2, B3 order, I4, I5, I6 ─────────────────────────────────────────────────

async def test_the_sql_sent_is_the_sealed_text_and_the_checks_run_in_order(execution_catalog):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    connection = _connection(catalog, records=[(1, None, "a")])
    remote, rows, _ = await _adapter_call(catalog, connection, sealed)

    # I2: the only model-derived text is the regenerated SQL, wrapped by the fixed per-row size guard.
    assert connection.prepared_sql == _wrap_row_size_guard(sealed.sql, catalog.settings.postgres_sql_max_row_bytes)
    wrapped = _wrap_row_size_guard(sealed.sql, catalog.settings.postgres_sql_max_row_bytes)
    assert connection.explained == [f"EXPLAIN (FORMAT JSON) {wrapped}"]
    assert "ANALYZE" not in connection.explained[0].upper().replace("EXPLAIN (FORMAT JSON)", "")
    lock_index = next(i for i, s in enumerate(connection.log) if s.startswith("LOCK TABLE"))
    prepare_index = connection.log.index(f"PREPARE {wrapped}")
    explain_index = next(i for i, s in enumerate(connection.log) if s.startswith("EXPLAIN"))
    assert lock_index < prepare_index < explain_index
    assert connection.transactions == [(True, "read_committed")]
    assert rows == [(1, None, "a")]
    assert remote.plan_cost == 100.0


@pytest.mark.parametrize(
    "type_name,hint",
    [("interval", "interval"), ("money", "money"), ("record", "composite"), ("orders", "other"), ("int4[]", "other")],
)
async def test_unsupported_output_types_stop_before_the_cursor_opens(execution_catalog, type_name, hint):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    kind = "composite" if type_name == "orders" else None
    attributes = [
        _attribute("id", "int8"),
        SimpleNamespace(name="x", type=SimpleNamespace(name=type_name, schema="pg_catalog" if type_name != "orders" else "sales", kind=kind)),
    ]
    connection = _connection(catalog, attributes=attributes, records=[(1, "boom")])
    with pytest.raises(PostgresExecutionUnsupportedOutputError) as caught:
        await _adapter_call(catalog, connection, sealed)
    assert caught.value.column_index == 2
    assert caught.value.hint == (hint if type_name != "orders" else "composite")
    assert connection.explained == []                                  # not even EXPLAIN
    assert connection.cursor.yielded == 0 and connection.statement.prefetch is None   # cursor never opened


async def test_cost_above_the_ceiling_is_refused_and_the_cursor_never_opens(execution_catalog):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    ceiling = catalog.settings.postgres_sql_max_plan_cost
    connection = _connection(catalog, cost=ceiling * 2, records=[(1, None, "a")])
    with pytest.raises(PostgresExecutionCostExceededError):
        await _adapter_call(catalog, connection, sealed)
    assert connection.cursor.yielded == 0 and connection.statement.prefetch is None

    at_ceiling = _connection(catalog, cost=ceiling, records=[(1, None, "a")])
    remote, rows, _ = await _adapter_call(catalog, at_ceiling, sealed)    # equal is allowed
    assert rows == [(1, None, "a")]


async def test_cost_ceiling_override_is_honoured(execution_catalog):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    connection = _connection(catalog, cost=1_664.0)
    with pytest.raises(PostgresExecutionCostExceededError):
        await _adapter_call(catalog, connection, sealed, max_plan_cost=1_000.0)


@pytest.mark.parametrize("payload", ["not json", "[]", '[{"Plan": {}}]', '[{"Plan": {"Total Cost": "x"}}]', '[{"Plan": {"Total Cost": NaN}}]'])
async def test_an_unreadable_explain_fails_closed(execution_catalog, payload):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    connection = _connection(catalog, records=[(1, None, "a")])

    async def bad(sql, *args):
        return payload

    connection.fetchval = bad
    with pytest.raises(PostgresExecutionAdapterError):
        await _adapter_call(catalog, connection, sealed)
    assert connection.cursor.yielded == 0


async def test_rows_are_read_by_position_so_duplicate_labels_never_collapse(execution_catalog):
    catalog = execution_catalog
    sealed = await _sealed(catalog, "SELECT count(*), count(*) FILTER (WHERE label = 'x') FROM sales.orders")

    class DupRecord(tuple):
        """Like asyncpg's Record: by-name lookup returns the LAST duplicate (F4)."""
        def __getitem__(self, key):
            if isinstance(key, str):
                return self[-1]
            return super().__getitem__(key)

        def keys(self):
            return ["count", "count"]

    connection = _connection(
        catalog,
        attributes=[_attribute("count", "int8"), _attribute("count", "int8")],
        records=[DupRecord((60000, 42020))],
    )
    remote, rows, _ = await _adapter_call(catalog, connection, sealed, expectation=_expectation(catalog, "label"))
    assert rows == [(60000, 42020)]                                    # I5
    assert [c.alias for c in remote.actual_output_columns] == ["count", "count_2"]


@pytest.mark.parametrize(
    "sql,model_cap_hit,expect_flag",
    [
        ("SELECT id FROM sales.orders", True, True),                   # no LIMIT -> cap + 1
        ("SELECT id FROM sales.orders LIMIT 100000", True, True),      # above the cap -> cap + 1
        ("SELECT id FROM sales.orders LIMIT 3", False, False),         # the model's own LIMIT
    ],
)
async def test_truncation_flag_only_for_the_safety_cap_shape(execution_catalog, sql, model_cap_hit, expect_flag):
    catalog = execution_catalog
    cap = catalog.settings.postgres_execution_max_result_rows
    sealed = await _sealed(catalog, sql)
    assert (sealed.applied_limit == cap + 1) is model_cap_hit
    n = sealed.applied_limit                                          # Postgres would return at most this many
    connection = _connection(catalog, attributes=[_attribute("id", "int8")], records=[(i,) for i in range(n)])
    remote, rows, _ = await _adapter_call(catalog, connection, sealed, expectation=_expectation(catalog, "id"))
    assert remote.safety_row_truncated is expect_flag
    assert len(rows) == min(n, cap)                                    # I6


async def test_a_limit_equal_to_the_cap_is_not_a_truncation(execution_catalog):
    catalog = execution_catalog
    cap = catalog.settings.postgres_execution_max_result_rows
    sealed = await _sealed(catalog, f"SELECT id FROM sales.orders LIMIT {cap}")
    assert sealed.applied_limit == cap
    connection = _connection(catalog, attributes=[_attribute("id", "int8")], records=[(i,) for i in range(cap)])
    remote, rows, _ = await _adapter_call(catalog, connection, sealed, expectation=_expectation(catalog, "id"))
    assert len(rows) == cap and remote.safety_row_truncated is False


async def test_cancellation_between_rows_terminates_the_connection(execution_catalog):
    from app.connectors.postgres_query_executor import PostgresExecutionCancellation, PostgresExecutionCancelledError

    catalog = execution_catalog
    sealed = await _sealed(catalog)
    cancellation = PostgresExecutionCancellation()
    connection = _connection(catalog, records=[(1, None, "a"), (2, None, "b")])

    async def consume(values):
        await cancellation.cancel()
        return True

    with pytest.raises(PostgresExecutionCancelledError):
        await _adapter_call(catalog, connection, sealed, consumer=consume, cancellation=cancellation)
    assert connection.terminated


# ── the service ──────────────────────────────────────────────────────────────

async def _text_columns_dump(factory) -> str:
    """Every value of every text-ish column of every app-DB table the run touches (I7)."""
    parts: list[str] = []
    async with factory() as session:
        for table in (PostgresQueryRun.__table__, PostgresQueryRunAsset.__table__):
            for row in (await session.execute(select(table))).mappings().all():
                parts.extend(str(v) for v in row.values())
    return "\n".join(parts)


async def test_a_run_succeeds_and_records_no_sql_text_or_literal(execution_catalog):
    catalog = execution_catalog
    connection = _connection(catalog, records=[(7, None, SECRET)])
    result, _ = await _run(catalog, connection)

    assert isinstance(result, SqlExecutionResult) and result.is_ok, result
    assert [(c.label, c.pg_type) for c in result.columns] == [("id", "int8"), ("amount", "numeric"), ("label", "text")]
    assert result.row_count == 1 and result.rows[0][0].value == "7" and result.rows[0][1].kind == "null"
    assert result.tables == (("sales", "orders"),)
    assert result.sql in connection.prepared_sql and SECRET in result.sql      # the SQL is returned to the caller
    assert result.plan_cost == 100.0 and result.safety_row_truncated is False
    assert result.delivery_binding is not None
    assert "delivery_binding" not in result.model_dump()

    async with catalog.factory() as session:
        run = (await session.execute(select(PostgresQueryRun))).scalar_one()
        assets = (await session.execute(select(PostgresQueryRunAsset))).scalars().all()
    assert run.id == result.run_id and run.query_kind == "sql" and run.asset_id is None
    assert run.status is PostgresQueryRunStatus.SUCCEEDED and run.returned_row_count == 1
    assert len(run.plan_fingerprint) == 64
    assert [a.asset_id for a in assets] == [catalog.asset_id]
    dump = await _text_columns_dump(catalog.factory)                          # I7
    assert SECRET not in dump and "SELECT" not in dump.upper() and "sales.orders" not in dump


async def test_the_fingerprint_stored_ignores_literals(execution_catalog):
    catalog = execution_catalog
    for secret in ("alpha", "beta"):
        await _run(catalog, _connection(catalog), f"SELECT id, amount, label FROM sales.orders WHERE label = '{secret}'")
    async with catalog.factory() as session:
        fingerprints = (await session.execute(select(PostgresQueryRun.plan_fingerprint))).scalars().all()
    assert len(fingerprints) == 2 and fingerprints[0] == fingerprints[1]


async def test_a_rejection_is_returned_as_is_and_creates_no_run(execution_catalog):
    catalog = execution_catalog
    connection = _connection(catalog)
    result, calls = await _run(catalog, connection, "DROP TABLE sales.orders")
    assert isinstance(result, Rejection)
    assert calls == []
    async with catalog.factory() as session:
        assert (await session.execute(select(PostgresQueryRun))).first() is None


async def test_a_caller_who_does_not_own_the_source_gets_a_safe_failure(execution_catalog):
    catalog = execution_catalog
    connection = _connection(catalog)
    result, calls = await _run(catalog, connection, user_id=catalog.other_id)
    assert isinstance(result, SqlExecutionResult) and result.status == "failed"
    assert result.failure_code == "query_not_authorized" and calls == []
    assert result.rows == ()


async def test_a_type_failure_is_recorded_and_the_message_is_fixed_text(execution_catalog):
    catalog = execution_catalog
    attributes = [_attribute("id", "int8"), SimpleNamespace(name="age", type=SimpleNamespace(name="interval", schema="pg_catalog", kind="scalar"))]
    connection = _connection(catalog, attributes=attributes, records=[(1, "secret-db-text")])
    result, _ = await _run(catalog, connection)
    assert result.status == "failed" and result.failure_code == "unsupported_output_type"
    assert result.message == "Output column 2 has a type that cannot be returned: use extract(epoch from ...) or subtract dates."
    assert result.rows == () and "secret-db-text" not in result.model_dump_json()
    async with catalog.factory() as session:
        run = (await session.execute(select(PostgresQueryRun))).scalar_one()
    assert run.status is PostgresQueryRunStatus.FAILED and run.failure_code == "unsupported_output_type"


async def test_a_cost_failure_maps_to_its_code(execution_catalog):
    catalog = execution_catalog
    connection = _connection(catalog, cost=1e9)
    result, _ = await _run(catalog, connection)
    assert result.failure_code == "query_too_expensive" and result.plan_cost is None
    assert "cross" in result.message.lower()


async def test_driver_text_never_reaches_the_result(execution_catalog):
    catalog = execution_catalog
    connection = _connection(catalog)

    async def boom(sql, *args):
        raise RuntimeError("password=hunter2 host=10.0.0.5 relation \"secret_table\"")

    connection.fetchval = boom
    result, _ = await _run(catalog, connection)
    assert result.failure_code == "execution_failed"
    blob = result.model_dump_json()
    assert "hunter2" not in blob and "10.0.0.5" not in blob and "secret_table" not in blob


@pytest.mark.parametrize(
    "exc",
    [
        PostgresExecutionUnsupportedOutputError(3, "money"),
        PostgresExecutionCostExceededError(),
        PostgresExecutionConfigurationError(),
        PostgresExecutionAdapterError(),
    ],
)
def test_adapter_errors_carry_no_text(exc):
    assert str(exc) == "" and exc.code and exc.__cause__ is None                    # I8


async def test_a_new_head_between_validation_and_execution_never_returns_stale_rows(execution_catalog):
    """The head moves after the SQL was validated: the old-snapshot rows are withheld."""
    from app.services.catalog_service import (
        CatalogTableIdentity,
        mark_catalog_sync_running,
        publish_catalog_snapshot,
        request_catalog_sync_for_owner,
    )
    from tests.catalog.test_postgres_query_executor import _snapshot_input

    catalog = execution_catalog
    connection = _connection(catalog, records=[(1, None, "a")])
    original_execute = connection.execute
    moved = False

    async def move_head_then_run(sql):
        nonlocal moved
        if not moved and sql.startswith("LOCK TABLE"):
            moved = True
            async with catalog.factory() as session:      # a real catalog refresh publishes a new head
                run = await request_catalog_sync_for_owner(
                    session, source_id=catalog.source_id, owner_user_id=catalog.owner_id
                )
                await mark_catalog_sync_running(session, source_id=catalog.source_id, run_id=run.id)
                await publish_catalog_snapshot(
                    session,
                    source_id=catalog.source_id,
                    run_id=run.id,
                    snapshot_input=_snapshot_input(CatalogTableIdentity("sales", "orders")),
                )
        await original_execute(sql)

    connection.execute = move_head_then_run
    result, _ = await _run(catalog, connection)
    assert moved
    assert result.status == "failed" and result.failure_code == "access_revoked"
    assert result.rows == ()
    assert "access" in result.message.lower()


async def test_cancellation_before_start_is_a_cancelled_result(execution_catalog):
    from app.connectors.postgres_query_executor import PostgresExecutionCancellation

    catalog = execution_catalog
    cancellation = PostgresExecutionCancellation()
    await cancellation.cancel()
    result, calls = await _run(catalog, _connection(catalog), cancellation=cancellation)
    assert result.status == "cancelled" and result.failure_code == "execution_cancelled" and calls == []
    async with catalog.factory() as session:
        run = (await session.execute(select(PostgresQueryRun))).scalar_one()
    assert run.status is PostgresQueryRunStatus.CANCELLED


# ── migration / model contract (B5) ──────────────────────────────────────────

async def _insert_run(session, catalog, **overrides):
    from datetime import datetime, timezone

    values = dict(
        id=uuid.uuid4(), owner_user_id=catalog.owner_id, source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id, asset_id=catalog.asset_id, plan_fingerprint="0" * 64,
        status=PostgresQueryRunStatus.RUNNING, requested_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc), result_truncated=False, result_complete=False,
        returned_row_count=0,
    )
    values.update(overrides)
    session.add(PostgresQueryRun(**values))
    await session.commit()
    return values["id"]


@pytest.mark.parametrize(
    "kind,asset,ok",
    [("compiled", True, True), ("compiled", False, False), ("sql", False, True), ("sql", True, False), ("other", False, False)],
)
async def test_query_kind_asset_check_matrix(execution_catalog, kind, asset, ok):
    catalog = execution_catalog
    async with catalog.factory() as session:
        attempt = _insert_run(
            session, catalog, query_kind=kind, asset_id=catalog.asset_id if asset else None
        )
        if ok:
            await attempt
        else:
            with pytest.raises(IntegrityError):
                await attempt


async def test_query_kind_defaults_to_compiled(execution_catalog):
    catalog = execution_catalog
    async with catalog.factory() as session:
        run_id = await _insert_run(session, catalog)
        run = await session.get(PostgresQueryRun, run_id)
        assert run.query_kind == "compiled"


async def test_run_assets_cascade_with_their_run_and_need_a_real_asset(execution_catalog):
    catalog = execution_catalog
    async with catalog.factory() as session:
        run_id = await _insert_run(session, catalog, query_kind="sql", asset_id=None)
        session.add(PostgresQueryRunAsset(run_id=run_id, asset_id=catalog.asset_id, source_id=catalog.source_id, snapshot_id=catalog.snapshot_id))
        await session.commit()
        await session.execute(delete(PostgresQueryRun).where(PostgresQueryRun.id == run_id))
        await session.commit()
        assert (await session.execute(select(PostgresQueryRunAsset))).first() is None

        run_id = await _insert_run(session, catalog, query_kind="sql", asset_id=None)
        session.add(PostgresQueryRunAsset(run_id=run_id, asset_id=uuid.uuid4(), source_id=catalog.source_id, snapshot_id=catalog.snapshot_id))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_run_asset_primary_key_is_run_and_asset(execution_catalog):
    catalog = execution_catalog
    async with catalog.factory() as session:
        run_id = await _insert_run(session, catalog, query_kind="sql", asset_id=None)
        for _ in range(2):
            session.add(PostgresQueryRunAsset(run_id=run_id, asset_id=catalog.asset_id, source_id=catalog.source_id, snapshot_id=catalog.snapshot_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                break
        else:
            pytest.fail("duplicate (run_id, asset_id) was accepted")


async def test_a_table_less_select_is_rejected_by_the_validator_and_never_connects(execution_catalog):
    """SPEC-05 decision: zero catalog table references -> ``no_table_reference``; nothing runs, no run row."""
    catalog = execution_catalog
    for sql in ("SELECT 1 AS one", "SELECT count(*) FROM generate_series(1, 10)"):
        result, calls = await _run(catalog, _connection(catalog), sql)
        assert isinstance(result, Rejection) and result.code.value == "no_table_reference"
        assert calls == []
    async with catalog.factory() as session:
        assert (await session.execute(select(PostgresQueryRun))).first() is None


async def test_the_adapter_also_requires_at_least_one_table(execution_catalog):
    """Defence in depth: even a hand-minted seal with no tables cannot be run (adapter requires 1..30)."""
    import importlib

    seal_module = importlib.import_module("app.services.sql_guard.seal")

    catalog = execution_catalog
    sealed = await _sealed(catalog)
    empty = dataclasses.replace(sealed, tables=(), columns=())
    object.__setattr__(empty, "_seal", seal_module._SEAL)        # as if the seal had been minted
    connection = _connection(catalog)
    with pytest.raises(PostgresExecutionConfigurationError):
        await _adapter_call(catalog, connection, empty, expectation=LiveSqlExpectation(relations=()))
    assert connection.log == []


async def test_more_than_the_maximum_number_of_relations_is_refused(execution_catalog):
    catalog = execution_catalog
    sealed = await _sealed(catalog)
    many = LiveSqlExpectation(relations=tuple(LiveRelationExpectation("sales", f"t{i}", ()) for i in range(31)))
    with pytest.raises(PostgresExecutionConfigurationError):
        await _adapter_call(catalog, _connection(catalog), sealed, expectation=many)


async def test_the_compiled_path_also_sets_the_client_connection_check(execution_catalog):
    """SPEC-05 decision 2: the shared session setup carries the cancellation fix for both paths."""
    from app.connectors.postgres_query_executor import _SESSION_CLIENT_CHECK_SQL
    from app.services.postgres_query_executor import execute_postgres_query
    from tests.catalog.test_postgres_query_executor import _plan, _record

    catalog = execution_catalog
    names = ("id",)
    connection = _connection(catalog, attributes=[_attribute("__nv_p0", "int8")], records=[_record(["__nv_p0"], [1])])
    adapter, _ = _adapter(catalog, connection)
    result = await execute_postgres_query(
        catalog.factory, owner_user_id=catalog.owner_id, source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id, plan=_plan(catalog, *names), settings=catalog.settings,
        adapter_factory=lambda _s: adapter,
    )
    assert result.status == "succeeded", result.failure_code
    assert _SESSION_CLIENT_CHECK_SQL in connection.log
    assert connection.log.index(_SESSION_CLIENT_CHECK_SQL) < next(
        i for i, s in enumerate(connection.log) if s.startswith("LOCK TABLE"))


async def test_the_sql_path_sets_it_before_locking_too(execution_catalog):
    from app.connectors.postgres_query_executor import _SESSION_CLIENT_CHECK_SQL

    catalog = execution_catalog
    connection = _connection(catalog, records=[(1, None, "a")])
    await _adapter_call(catalog, connection, await _sealed(catalog))
    assert connection.log.count(_SESSION_CLIENT_CHECK_SQL) == 1
    assert connection.log.index(_SESSION_CLIENT_CHECK_SQL) < next(
        i for i, s in enumerate(connection.log) if s.startswith("LOCK TABLE"))


def test_the_row_size_guard_wraps_the_sql_and_keeps_the_branch_row_dependent():
    wrapped = _wrap_row_size_guard("SELECT a, a FROM t ORDER BY 1", 4096)
    assert wrapped.startswith("SELECT q.* FROM (SELECT a, a FROM t ORDER BY 1) AS q WHERE CASE WHEN ")
    assert "octet_length(ROW(q.*)::text) > 4096" in wrapped
    # The failing branch must reference the row, or the planner folds the constant and fails every query.
    assert "CAST('nn_row_too_large:' || octet_length(ROW(q.*)::text) AS int)" in wrapped


async def test_a_server_refused_oversized_row_becomes_result_row_too_large(execution_catalog):
    import asyncpg

    catalog = execution_catalog
    sealed = await _sealed(catalog)
    connection = _connection(catalog, records=[(1, None, "a")])
    error = asyncpg.InvalidTextRepresentationError('invalid input syntax for type integer: "nn_row_too_large:2000000"')

    async def boom(sql: str, *a: Any):
        raise error

    connection.fetchval = boom   # surfaces at the first statement that touches the wrapped SQL
    with pytest.raises(PostgresExecutionRowTooLargeError) as caught:
        await _adapter_call(catalog, connection, sealed)
    assert caught.value.code == "result_row_too_large" and "2000000" not in str(caught.value)


async def test_other_invalid_text_errors_are_not_mistaken_for_oversize(execution_catalog):
    import asyncpg

    catalog = execution_catalog
    sealed = await _sealed(catalog)
    connection = _connection(catalog, records=[])

    async def boom(sql: str, *a: Any):
        raise asyncpg.InvalidTextRepresentationError('invalid input syntax for type integer: "x"')

    connection.fetchval = boom
    with pytest.raises(PostgresExecutionAdapterError) as caught:
        await _adapter_call(catalog, connection, sealed)
    assert not isinstance(caught.value, PostgresExecutionRowTooLargeError)
