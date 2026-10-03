"""Focused Stage 4B controlled PostgreSQL execution contracts.

The application catalog is real SQLite state.  The client PostgreSQL side is
an intentionally narrow asyncpg-shaped fake: it exercises the execution
adapter's session policy and streaming boundary without claiming that SQLite
or a fake driver proves PostgreSQL behaviour.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.connection_policy import PostgresConnectionPolicy
from app.connectors.postgres_query_executor import (
    ActualPostgresOutputColumn,
    PostgresExecutionCancellation,
    PostgresExecutionConfigurationError,
    PostgresExecutionTimeoutError,
    PostgresQueryExecutionAdapter,
    RemotePostgresExecution,
)
from app.core.config import Settings
from app.core.connector_credentials import CredentialBundle, CredentialProtector
from app.db.base import Base
from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogDeclaredKey,
    CatalogDeclaredKeyColumn,
    CatalogForeignKey,
    CatalogForeignKeyColumn,
    CatalogSnapshot,
    CatalogSyncRun,
    SourceCatalogHead,
)
from app.db.models.conversation import ConversationSessionRow
from app.db.models.dataset import DatasetRecord
from app.db.models.postgres_query_run import PostgresQueryRun, PostgresQueryRunStatus
from app.db.models.source import (
    DataSource,
    SourceCredential,
    SourceStatus,
    SourceTableScope,
    SourceType,
    TLSMode,
)
from app.db.models.user import User, UserRole
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogColumnInput,
    CatalogSnapshotInput,
    CatalogTableIdentity,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    request_catalog_sync_for_owner,
)
import app.services.postgres_query_executor as postgres_query_executor_service
from app.services.postgres_query_compiler import (
    CompiledPostgresQuery,
    validate_and_compile_postgres_query,
)
from app.services.postgres_query_executor import PostgresExecutionLimiter, execute_postgres_query
from app.services.postgres_query_plan import PostgresQueryPlan


_TABLES = [
    User.__table__,
    DatasetRecord.__table__,
    ConversationSessionRow.__table__,
    DataSource.__table__,
    SourceCredential.__table__,
    SourceTableScope.__table__,
    CatalogSyncRun.__table__,
    CatalogSnapshot.__table__,
    SourceCatalogHead.__table__,
    CatalogAsset.__table__,
    CatalogColumn.__table__,
    CatalogDeclaredKey.__table__,
    CatalogDeclaredKeyColumn.__table__,
    CatalogForeignKey.__table__,
    CatalogForeignKeyColumn.__table__,
    PostgresQueryRun.__table__,
]


@dataclass(frozen=True)
class _ExecutionCatalog:
    factory: async_sessionmaker
    settings: Settings
    owner_id: uuid.UUID
    other_id: uuid.UUID
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    columns: dict[str, CatalogColumn]


@pytest.fixture
async def execution_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_TABLES)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def execution_settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage4b-execution-test-key",
        database_url="sqlite+aiosqlite://",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id="stage4b-test-key",
        connector_credentials_active_key=Fernet.generate_key().decode(),
        postgres_execution_cursor_prefetch=1,
    )


def _column(name: str, ordinal: int, *, native_type_name: str, native_type_oid: int) -> CatalogColumnInput:
    return CatalogColumnInput(
        column_name=name,
        ordinal_position=ordinal,
        is_nullable=name == "label",
        formatted_native_type=native_type_name,
        native_type_schema="pg_catalog",
        native_type_name=native_type_name,
        native_type_oid=native_type_oid,
        native_type_modifier=-1,
        column_comment=None,
    )


def _snapshot_input(identity: CatalogTableIdentity) -> CatalogSnapshotInput:
    return CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=identity,
                columns=(
                    _column("id", 1, native_type_name="int8", native_type_oid=20),
                    _column("amount", 2, native_type_name="numeric", native_type_oid=1700),
                    _column("label", 3, native_type_name="text", native_type_oid=25),
                    _column("order_date", 4, native_type_name="date", native_type_oid=1082),
                    _column("created_at", 5, native_type_name="timestamp", native_type_oid=1114),
                    _column("event_at", 6, native_type_name="timestamptz", native_type_oid=1184),
                    _column("external_id", 7, native_type_name="uuid", native_type_oid=2950),
                    _column("ratio", 8, native_type_name="float8", native_type_oid=701),
                ),
            ),
        )
    )


async def _make_user(session, email: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        name=email.split("@", 1)[0],
        role=UserRole.USER,
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def execution_catalog(
    execution_session_factory: async_sessionmaker,
    execution_settings: Settings,
) -> _ExecutionCatalog:
    async with execution_session_factory() as session:
        owner = await _make_user(session, "owner@example.test")
        other = await _make_user(session, "other@example.test")
        source_id = uuid.uuid4()
        credential = CredentialProtector.from_settings(execution_settings).encrypt(
            CredentialBundle(
                source_id=source_id,
                username="restricted_reader",
                password="synthetic-source-password",
                custom_ca_pem=None,
            )
        )
        identity = CatalogTableIdentity("sales", "orders")
        source = DataSource(
            id=source_id,
            owner_user_id=owner.id,
            source_type=SourceType.POSTGRESQL,
            display_name="Stage 4B synthetic source",
            host="client-postgres.stage4b.test",
            port=5432,
            database_name="analytics",
            tls_mode=TLSMode.VERIFY_FULL,
            has_custom_ca=False,
            status=SourceStatus.TESTED,
            profiling_enabled=False,
            catalog_sync_sequence=0,
            credential=SourceCredential(
                ciphertext=credential[0], key_id=credential[1], encryption_format=credential[2]
            ),
            table_scopes=[SourceTableScope(schema_name=identity.schema_name, table_name=identity.table_name)],
        )
        session.add(source)
        await session.commit()
        run = await request_catalog_sync_for_owner(
            session, source_id=source.id, owner_user_id=owner.id
        )
        await mark_catalog_sync_running(session, source_id=source.id, run_id=run.id)
        snapshot = await publish_catalog_snapshot(
            session,
            source_id=source.id,
            run_id=run.id,
            snapshot_input=_snapshot_input(identity),
        )
        asset = (
            await session.execute(
                select(CatalogAsset).where(
                    CatalogAsset.source_id == source.id,
                    CatalogAsset.snapshot_id == snapshot.id,
                )
            )
        ).scalar_one()
        columns = list(
            (
                await session.execute(
                    select(CatalogColumn)
                    .where(
                        CatalogColumn.source_id == source.id,
                        CatalogColumn.snapshot_id == snapshot.id,
                        CatalogColumn.asset_id == asset.id,
                    )
                    .order_by(CatalogColumn.ordinal_position)
                )
            ).scalars()
        )
        return _ExecutionCatalog(
            factory=execution_session_factory,
            settings=execution_settings,
            owner_id=owner.id,
            other_id=other.id,
            source_id=source.id,
            snapshot_id=snapshot.id,
            asset_id=asset.id,
            columns={column.column_name: column for column in columns},
        )


def _plan(catalog: _ExecutionCatalog, *column_names: str, limit: int = 5, **overrides: Any) -> PostgresQueryPlan:
    values: dict[str, Any] = {
        "asset_id": catalog.asset_id,
        "projections": [{"column_id": catalog.columns[name].id} for name in column_names],
        "limit": limit,
    }
    values.update(overrides)
    return PostgresQueryPlan.model_validate(values)


def _live_column(column: CatalogColumn) -> dict[str, Any]:
    return {
        "column_name": column.column_name,
        "native_type_schema": column.native_type_schema,
        "native_type_name": column.native_type_name,
        "native_type_oid": column.native_type_oid,
        "native_type_modifier": column.native_type_modifier,
        "is_nullable": column.is_nullable,
        "has_column_select": True,
    }


def _relation_row(**overrides: Any) -> dict[str, Any]:
    values = {
        "relation_oid": 481516,
        "schema_name": "sales",
        "table_name": "orders",
        "relation_kind": "r",
        "is_partition": False,
        "has_inheritance_children": False,
        "has_inheritance_membership": False,
        "row_security_enabled": False,
        "force_row_security": False,
        "role_is_superuser": False,
        "role_bypasses_rls": False,
        "role_is_table_owner": False,
        "has_schema_usage": True,
        "has_table_select": True,
    }
    values.update(overrides)
    return values


def _session_assertion_row(**overrides: Any) -> dict[str, str]:
    values = {
        "transaction_read_only": "on",
        "search_path": "pg_catalog",
        "time_zone": "UTC",
    }
    values.update(overrides)
    return values


class _Transaction:
    def __init__(self, connection: "_Connection", *, readonly: bool, isolation: str):
        self._connection = connection
        self._readonly = readonly
        self._isolation = isolation

    async def __aenter__(self):
        self._connection.transactions.append((self._readonly, self._isolation))
        return self

    async def __aexit__(self, *_args):
        return False


class _Cursor:
    def __init__(self, records: list[dict[str, Any]], *, block: bool = False):
        self._records = list(records)
        self._index = 0
        self._block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.yielded = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._block and not self.started.is_set():
            self.started.set()
            await self.release.wait()
        if self._index >= len(self._records):
            raise StopAsyncIteration
        value = self._records[self._index]
        self._index += 1
        self.yielded += 1
        return value


class _Statement:
    def __init__(self, attributes: list[Any], cursor: _Cursor):
        self._attributes = attributes
        self._cursor = cursor
        self.parameters: tuple[Any, ...] | None = None
        self.prefetch: int | None = None

    def get_attributes(self):
        return self._attributes

    def cursor(self, *parameters: Any, prefetch: int):
        self.parameters = parameters
        self.prefetch = prefetch
        return self._cursor


class _Connection:
    def __init__(
        self,
        *,
        live_columns: dict[str, dict[str, Any]],
        attributes: list[Any],
        records: list[dict[str, Any]],
        relation: dict[str, Any] | None = None,
        relation_after_lock: dict[str, Any] | None = None,
        session_assertions: dict[str, str] | None = None,
        block_cursor: bool = False,
    ) -> None:
        self.live_columns = live_columns
        self.relation = relation or _relation_row()
        self.relation_after_lock = relation_after_lock
        self._relation_fetch_count = 0
        self.session_assertions = session_assertions or _session_assertion_row()
        self.executed: list[str] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.prepared_sql: str | None = None
        self.transactions: list[tuple[bool, str]] = []
        self.closed = False
        self.terminated = False
        self.cursor = _Cursor(records, block=block_cursor)
        self.statement = _Statement(attributes, self.cursor)

    def transaction(self, *, readonly: bool, isolation: str):
        return _Transaction(self, readonly=readonly, isolation=isolation)

    async def execute(self, sql: str):
        self.executed.append(sql)

    async def fetchrow(self, sql: str, *parameters: Any):
        self.fetchrow_calls.append((sql, parameters))
        if "transaction_read_only" in sql:
            return dict(self.session_assertions)
        self._relation_fetch_count += 1
        if self._relation_fetch_count > 1 and self.relation_after_lock is not None:
            return dict(self.relation_after_lock)
        return dict(self.relation)

    async def fetch(self, sql: str, *parameters: Any):
        self.fetch_calls.append((sql, parameters))
        names = parameters[1]
        return [dict(self.live_columns[name]) for name in names]

    async def prepare(self, sql: str):
        self.prepared_sql = sql
        return self.statement

    async def terminate(self):
        self.terminated = True
        self.cursor.release.set()

    async def close(self):
        self.closed = True


def _attribute(alias: str, postgres_type: str) -> Any:
    return SimpleNamespace(name=alias, type=SimpleNamespace(name=postgres_type, schema="pg_catalog"))


def _record(aliases: list[str], values: list[Any]) -> dict[str, Any]:
    return dict(zip(aliases, values, strict=True))


async def _resolver(host: str, port: int) -> list[str]:
    assert host == "client-postgres.stage4b.test"
    assert port == 5432
    return ["127.0.0.1"]


def _adapter_with_connection(settings: Settings, connection: _Connection):
    calls: list[dict[str, Any]] = []

    async def connect(**kwargs: Any):
        calls.append(kwargs)
        return connection

    return (
        PostgresQueryExecutionAdapter(
            settings=settings,
            policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
            connect=connect,
        ),
        calls,
    )


async def test_execute_returns_typed_values_and_enforces_client_session_policy(execution_catalog):
    catalog = execution_catalog
    names = (
        "id",
        "amount",
        "label",
        "order_date",
        "created_at",
        "event_at",
        "external_id",
        "ratio",
    )
    aliases = [f"__nv_p{index}" for index in range(len(names))]
    attributes = [_attribute(alias, catalog.columns[name].native_type_name) for alias, name in zip(aliases, names)]
    values = [
        9_007_199_254_740_993,
        Decimal("123.4500"),
        "North",
        date(2026, 1, 2),
        datetime(2026, 1, 2, 3, 4, 5),
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        uuid.UUID("7a82a13d-5e92-4a18-8f2d-5d5fe1b7bd65"),
        float("nan"),
    ]
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=attributes,
        records=[_record(aliases, values)],
    )
    adapter, connect_calls = _adapter_with_connection(catalog.settings, connection)

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, *names),
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.failure_code is None
    assert result.status == "succeeded"
    assert result.complete is True
    assert result.returned_row_count == 1
    assert [value.kind for value in result.rows[0]] == [
        "integer",
        "decimal",
        "text",
        "date",
        "timestamp",
        "timestamptz",
        "uuid",
        "non_finite_number",
    ]
    assert result.rows[0][0].value == "9007199254740993"
    assert result.rows[0][1].value == "123.4500"
    assert result.rows[0][5].value == "2026-01-01T21:34:05Z"
    assert result.rows[0][7].value == "NaN"

    assert len(connect_calls) == 1
    connect_kwargs = connect_calls[0]
    assert connect_kwargs["host"] == "client-postgres.stage4b.test"
    assert connect_kwargs["user"] == "restricted_reader"
    assert connect_kwargs["password"] == "synthetic-source-password"
    assert connect_kwargs["ssl"].check_hostname is True
    assert connect_kwargs["server_settings"] == {
        "application_name": "neuronova-postgres-query",
        "default_transaction_read_only": "on",
        "search_path": "pg_catalog",
        "TimeZone": "UTC",
        "statement_timeout": "5000",
        "lock_timeout": "2000",
        "idle_in_transaction_session_timeout": "10000",
    }
    assert connection.transactions == [(True, "read_committed")]
    assert connection.executed[:2] == [
        "SET LOCAL search_path TO pg_catalog",
        "SET LOCAL TimeZone TO 'UTC'",
    ]
    assert connection.executed[2:5] == [
        "SET LOCAL statement_timeout TO '5000ms'",
        "SET LOCAL lock_timeout TO '2000ms'",
        "SET LOCAL idle_in_transaction_session_timeout TO '10000ms'",
    ]
    assert any("current_setting('transaction_read_only')" in sql for sql, _params in connection.fetchrow_calls)
    assert any(statement.startswith('LOCK TABLE "sales"."orders" IN ACCESS SHARE MODE') for statement in connection.executed)
    assert connection.statement.parameters == (5,)
    assert connection.statement.prefetch == 1
    assert connection.closed is True
    assert connection.terminated is False


@pytest.mark.parametrize(
    "session_assertions",
    [
        _session_assertion_row(transaction_read_only="off"),
        _session_assertion_row(search_path="public, pg_catalog"),
        _session_assertion_row(time_zone="America/New_York"),
    ],
)
async def test_execution_rejects_misconfigured_client_session_before_query(
    execution_catalog, session_assertions
):
    catalog = execution_catalog
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[_record(["__nv_p0"], [1])],
        session_assertions=session_assertions,
    )
    adapter, _calls = _adapter_with_connection(catalog.settings, connection)

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.status == "failed"
    assert result.failure_code == "unsupported_execution_configuration"
    assert connection.prepared_sql is None
    assert len(connection.fetchrow_calls) == 1
    assert connection.terminated is True
    assert connection.closed is True


async def test_execution_rejects_raw_or_foreign_plan_before_remote_connect(execution_catalog):
    catalog = execution_catalog
    called = False

    def no_adapter(_settings):
        nonlocal called
        called = True
        raise AssertionError("untyped input must not construct a remote adapter")

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan="SELECT * FROM sales.orders",  # type: ignore[arg-type]
        settings=catalog.settings,
        adapter_factory=no_adapter,
    )

    assert result.status == "failed"
    assert result.failure_code == "invalid_query_plan"
    assert called is False

    # A previously authorized compiler artifact is not a durable permission
    # and cannot be supplied to the execution API in place of a typed plan.
    async with catalog.factory() as session:
        precompiled = await validate_and_compile_postgres_query(
            session,
            owner_user_id=catalog.owner_id,
            source_id=catalog.source_id,
            snapshot_id=catalog.snapshot_id,
            plan=_plan(catalog, "id"),
            settings=catalog.settings,
        )
    compiled_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=precompiled,  # type: ignore[arg-type]
        settings=catalog.settings,
        adapter_factory=no_adapter,
    )

    assert compiled_result.status == "failed"
    assert compiled_result.failure_code == "invalid_query_plan"
    assert called is False


async def test_transport_rejects_hand_forged_compiled_query_before_connect(execution_catalog):
    catalog = execution_catalog
    async with catalog.factory() as session:
        minted = await validate_and_compile_postgres_query(
            session,
            owner_user_id=catalog.owner_id,
            source_id=catalog.source_id,
            snapshot_id=catalog.snapshot_id,
            plan=_plan(catalog, "id"),
            settings=catalog.settings,
        )
    # The structure is deliberately valid-looking, but it was not minted by
    # the compiler and must not become a way to carry caller-provided SQL.
    forged_queries = (
        CompiledPostgresQuery(
            sql="SELECT pg_catalog.current_user",
            parameters=(),
            source_id=minted.source_id,
            snapshot_id=minted.snapshot_id,
            asset_id=minted.asset_id,
            expected_output_columns=minted.expected_output_columns,
            effective_limit=minted.effective_limit,
            ordering=minted.ordering,
            semantics=minted.semantics,
            fingerprint=minted.fingerprint,
        ),
        # Dataclass cloning must not retain the compiler-only capability.
        replace(minted, sql="SELECT pg_catalog.current_user"),
    )
    connection = _Connection(live_columns={}, attributes=[], records=[])
    adapter, connect_calls = _adapter_with_connection(catalog.settings, connection)

    for forged in forged_queries:
        with pytest.raises(PostgresExecutionConfigurationError):
            await adapter.execute(
                host="client-postgres.stage4b.test",
                port=5432,
                database_name="analytics",
                username="restricted_reader",
                password="synthetic-source-password",
                custom_ca_pem=None,
                compiled=forged,
                expectation=None,  # type: ignore[arg-type]
                max_rows=1,
                row_consumer=lambda _row: True,
            )

    assert connect_calls == []


async def test_execution_rejects_other_owner_before_remote_connect(execution_catalog):
    catalog = execution_catalog
    called = False

    def no_adapter(_settings):
        nonlocal called
        called = True
        raise AssertionError("authorization failure must not contact client PostgreSQL")

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.other_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=no_adapter,
    )

    assert result.status == "failed"
    assert result.failure_code == "query_not_authorized"
    assert called is False


async def test_execution_rejects_wrong_source_snapshot_and_scope_before_remote_connect(
    execution_catalog,
):
    catalog = execution_catalog
    called = False

    def no_adapter(_settings):
        nonlocal called
        called = True
        raise AssertionError("invalid source context must not contact client PostgreSQL")

    for source_id, snapshot_id in (
        (uuid.uuid4(), catalog.snapshot_id),
        (catalog.source_id, uuid.uuid4()),
    ):
        result = await execute_postgres_query(
            catalog.factory,
            owner_user_id=catalog.owner_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            plan=_plan(catalog, "id"),
            settings=catalog.settings,
            adapter_factory=no_adapter,
        )
        assert result.status == "failed"
        assert result.failure_code == "query_not_authorized"

    async with catalog.factory() as session:
        await session.execute(
            delete(SourceTableScope).where(SourceTableScope.source_id == catalog.source_id)
        )
        await session.commit()
    scope_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=no_adapter,
    )

    assert scope_result.status == "failed"
    assert scope_result.failure_code == "query_not_authorized"
    assert called is False


async def test_execution_distinguishes_safety_row_and_byte_truncation(execution_catalog):
    catalog = execution_catalog
    row_settings = catalog.settings.model_copy(update={"postgres_execution_max_result_rows": 1})
    row_connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[_record(["__nv_p0"], [1]), _record(["__nv_p0"], [2])],
    )
    row_adapter, _calls = _adapter_with_connection(row_settings, row_connection)
    row_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id", limit=5),
        settings=row_settings,
        adapter_factory=lambda _settings: row_adapter,
    )

    assert row_result.status == "succeeded", row_result.model_dump()
    assert row_result.requested_limit == 5
    assert row_result.safety_row_limit == 1
    assert row_result.safety_row_truncated is True
    assert row_result.safety_byte_truncated is False
    assert row_result.complete is False
    assert [row[0].value for row in row_result.rows] == ["1"]
    # One bounded lookahead establishes that more materialized output exists.
    assert row_connection.cursor.yielded == 2

    byte_settings = catalog.settings.model_copy(update={"postgres_execution_max_result_bytes": 1024})
    oversized = "x" * 2048
    byte_connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "text")],
        records=[_record(["__nv_p0"], [oversized])],
    )
    byte_adapter, _calls = _adapter_with_connection(byte_settings, byte_connection)
    byte_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "label", limit=5),
        settings=byte_settings,
        adapter_factory=lambda _settings: byte_adapter,
    )

    assert byte_result.status == "succeeded", byte_result.model_dump()
    assert byte_result.rows == ()
    assert byte_result.safety_row_truncated is False
    assert byte_result.safety_byte_truncated is True
    assert byte_result.complete is False
    assert oversized not in byte_result.model_dump_json()


async def test_requested_top_n_is_not_reported_as_safety_truncation(execution_catalog):
    catalog = execution_catalog
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        # A real compiled SQL LIMIT 1 can return only this one row.
        records=[_record(["__nv_p0"], [1])],
    )
    adapter, _calls = _adapter_with_connection(catalog.settings, connection)

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(
            catalog,
            "id",
            limit=1,
            sort={"output_kind": "projection", "output_index": 0, "direction": "asc"},
        ),
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.status == "succeeded", result.model_dump()
    assert result.explicit_top_n is True
    assert result.requested_limit == 1
    assert result.safety_row_limit == catalog.settings.postgres_execution_max_result_rows
    assert result.safety_row_truncated is False
    assert result.safety_byte_truncated is False
    assert result.complete is True


async def test_empty_result_is_a_success_with_expected_columns(execution_catalog):
    catalog = execution_catalog
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[],
    )
    adapter, _calls = _adapter_with_connection(catalog.settings, connection)

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.status == "succeeded", result.model_dump()
    assert result.failure_code is None
    assert result.rows == ()
    assert result.returned_row_count == 0
    assert result.complete is True
    assert result.columns[0].alias == "__nv_p0"


async def test_revoked_scope_after_remote_work_discards_rows_before_delivery(execution_catalog):
    catalog = execution_catalog

    class _RevokingAdapter:
        calls = 0

        async def execute(self, *, compiled, row_consumer, **_kwargs):
            self.calls += 1
            assert await row_consumer((42,)) is True
            async with catalog.factory() as session:
                await session.execute(
                    delete(SourceTableScope).where(SourceTableScope.source_id == catalog.source_id)
                )
                await session.commit()
            return RemotePostgresExecution(
                actual_output_columns=(
                    ActualPostgresOutputColumn(
                        alias=compiled.expected_output_columns[0].alias,
                        postgres_type="int8",
                        postgres_type_schema="pg_catalog",
                    ),
                ),
                safety_row_truncated=False,
                collector_stopped=False,
            )

    adapter = _RevokingAdapter()
    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert adapter.calls == 1
    assert result.status == "failed"
    assert result.failure_code == "access_revoked"
    assert result.rows == ()
    assert result.warnings == ("result_not_exposed",)


async def test_deactivated_source_after_remote_work_discards_rows_before_delivery(execution_catalog):
    catalog = execution_catalog

    class _DeactivatingAdapter:
        async def execute(self, *, compiled, row_consumer, **_kwargs):
            assert await row_consumer((42,)) is True
            async with catalog.factory() as session:
                source = await session.get(DataSource, catalog.source_id)
                assert source is not None
                source.status = SourceStatus.CONNECTION_FAILED
                await session.commit()
            return RemotePostgresExecution(
                actual_output_columns=(
                    ActualPostgresOutputColumn(
                        alias=compiled.expected_output_columns[0].alias,
                        postgres_type="int8",
                        postgres_type_schema="pg_catalog",
                    ),
                ),
                safety_row_truncated=False,
                collector_stopped=False,
            )

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: _DeactivatingAdapter(),
    )

    assert result.status == "failed"
    assert result.failure_code == "access_revoked"
    assert result.rows == ()
    assert result.warnings == ("result_not_exposed",)


async def test_execution_run_is_redacted_and_finalized_without_retry(execution_catalog):
    catalog = execution_catalog
    bound_secret = "secret filter value; SELECT pg_sleep(10)"
    plan = _plan(
        catalog,
        "label",
        filters=(
            {
                "column_id": catalog.columns["label"].id,
                "operator": "contains",
                "value": {"kind": "text", "value": bound_secret},
            },
        ),
    )
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "text")],
        records=[_record(["__nv_p0"], ["visible result"])],
    )
    adapter, connect_calls = _adapter_with_connection(catalog.settings, connection)
    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=plan,
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.status == "succeeded", result.model_dump()
    assert len(connect_calls) == 1
    async with catalog.factory() as session:
        run = await session.get(PostgresQueryRun, result.execution_id)
    assert run is not None
    assert run.status is PostgresQueryRunStatus.SUCCEEDED
    assert run.owner_user_id == catalog.owner_id
    assert run.source_id == catalog.source_id
    assert run.snapshot_id == catalog.snapshot_id
    assert run.asset_id == catalog.asset_id
    assert run.result_complete is True
    assert run.returned_row_count == 1
    assert {"sql", "parameters", "rows", "password", "host"}.isdisjoint(
        PostgresQueryRun.__table__.columns.keys()
    )
    assert bound_secret not in repr(run)
    assert bound_secret not in result.safe_provenance().values()


async def test_terminal_persistence_failure_withholds_completed_rows_without_retry(
    execution_catalog, monkeypatch
):
    catalog = execution_catalog
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[_record(["__nv_p0"], [42])],
    )
    adapter, connect_calls = _adapter_with_connection(catalog.settings, connection)

    async def fail_finalization(*_args, **_kwargs):
        raise postgres_query_executor_service._ExecutionBoundaryError("execution_recording_failed")

    monkeypatch.setattr(postgres_query_executor_service, "_finalize_run", fail_finalization)
    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.status == "failed"
    assert result.failure_code == "execution_recording_failed"
    assert result.rows == ()
    assert len(connect_calls) == 1
    async with catalog.factory() as session:
        run = await session.get(PostgresQueryRun, result.execution_id)
    assert run is not None
    assert run.status is PostgresQueryRunStatus.RUNNING


async def test_internal_cancellation_terminates_dedicated_connection_and_returns_safe_result(execution_catalog):
    catalog = execution_catalog
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[_record(["__nv_p0"], [1])],
        block_cursor=True,
    )
    adapter, _calls = _adapter_with_connection(catalog.settings, connection)
    cancellation = PostgresExecutionCancellation()
    task = asyncio.create_task(
        execute_postgres_query(
            catalog.factory,
            owner_user_id=catalog.owner_id,
            source_id=catalog.source_id,
            snapshot_id=catalog.snapshot_id,
            plan=_plan(catalog, "id"),
            settings=catalog.settings,
            adapter_factory=lambda _settings: adapter,
            cancellation=cancellation,
        )
    )
    await asyncio.wait_for(connection.cursor.started.wait(), timeout=1)
    await cancellation.cancel()
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status == "cancelled"
    assert result.failure_code == "execution_cancelled"
    assert result.rows == ()
    assert connection.terminated is True
    assert connection.closed is True


async def test_caller_task_cancellation_terminates_dedicated_connection(execution_catalog):
    catalog = execution_catalog
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[_record(["__nv_p0"], [1])],
        block_cursor=True,
    )
    adapter, _calls = _adapter_with_connection(catalog.settings, connection)
    task = asyncio.create_task(
        execute_postgres_query(
            catalog.factory,
            owner_user_id=catalog.owner_id,
            source_id=catalog.source_id,
            snapshot_id=catalog.snapshot_id,
            plan=_plan(catalog, "id"),
            settings=catalog.settings,
            adapter_factory=lambda _settings: adapter,
        )
    )
    await asyncio.wait_for(connection.cursor.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.terminated is True
    assert connection.closed is True


async def test_late_cancellation_discards_rows_before_delivery(execution_catalog):
    catalog = execution_catalog
    cancellation = PostgresExecutionCancellation()

    class _LateCancellingAdapter:
        async def execute(self, *, compiled, row_consumer, cancellation, **_kwargs):
            assert cancellation is not None
            assert await row_consumer((42,)) is True
            await cancellation.cancel()
            return RemotePostgresExecution(
                actual_output_columns=(
                    ActualPostgresOutputColumn(
                        alias=compiled.expected_output_columns[0].alias,
                        postgres_type="int8",
                        postgres_type_schema="pg_catalog",
                    ),
                ),
                safety_row_truncated=False,
                collector_stopped=False,
            )

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: _LateCancellingAdapter(),
        cancellation=cancellation,
    )

    assert result.status == "cancelled"
    assert result.failure_code == "execution_cancelled"
    assert result.rows == ()


async def test_live_schema_access_and_output_shape_changes_fail_closed(execution_catalog):
    catalog = execution_catalog

    view_connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[],
        relation=_relation_row(relation_kind="v"),
    )
    view_adapter, _calls = _adapter_with_connection(catalog.settings, view_connection)
    view_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: view_adapter,
    )
    assert view_result.status == "failed"
    assert view_result.failure_code == "schema_changed"
    assert view_result.rows == ()
    assert view_connection.terminated is True

    changed_relation_connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[],
        # Simulates a replacement observed after ACCESS SHARE acquisition.
        relation_after_lock=_relation_row(relation_oid=999_999),
    )
    changed_relation_adapter, _calls = _adapter_with_connection(
        catalog.settings, changed_relation_connection
    )
    changed_relation_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: changed_relation_adapter,
    )
    assert changed_relation_result.status == "failed"
    assert changed_relation_result.failure_code == "schema_changed"
    assert changed_relation_connection.prepared_sql is None

    privileged_connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[],
        relation=_relation_row(role_is_superuser=True),
    )
    privileged_adapter, _calls = _adapter_with_connection(catalog.settings, privileged_connection)
    privileged_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: privileged_adapter,
    )
    assert privileged_result.status == "failed"
    assert privileged_result.failure_code == "restricted_role_required"
    assert privileged_result.rows == ()

    inherited_connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[],
        # An inheritance child has no subclasses of its own, so relhassubclass
        # alone is insufficient to recognize it as unsupported.
        relation=_relation_row(has_inheritance_membership=True),
    )
    inherited_adapter, _calls = _adapter_with_connection(catalog.settings, inherited_connection)
    inherited_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: inherited_adapter,
    )
    assert inherited_result.status == "failed"
    assert inherited_result.failure_code == "schema_changed"
    assert inherited_result.rows == ()

    denied = _live_column(catalog.columns["id"])
    denied["has_column_select"] = False
    denied_connection = _Connection(
        live_columns={"id": denied},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[],
    )
    denied_adapter, _calls = _adapter_with_connection(catalog.settings, denied_connection)
    denied_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: denied_adapter,
    )
    assert denied_result.status == "failed"
    assert denied_result.failure_code == "access_changed"
    assert denied_result.rows == ()

    wrong_live_type = _live_column(catalog.columns["id"])
    wrong_live_type["native_type_oid"] = 23
    wrong_live_type_connection = _Connection(
        live_columns={"id": wrong_live_type},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[],
    )
    wrong_live_type_adapter, _calls = _adapter_with_connection(
        catalog.settings, wrong_live_type_connection
    )
    wrong_live_type_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: wrong_live_type_adapter,
    )
    assert wrong_live_type_result.status == "failed"
    assert wrong_live_type_result.failure_code == "schema_changed"
    assert wrong_live_type_result.rows == ()

    wrong_output_connection = _Connection(
        live_columns={"id": _live_column(catalog.columns["id"])},
        attributes=[_attribute("unexpected_alias", "int8")],
        records=[],
    )
    wrong_output_adapter, _calls = _adapter_with_connection(catalog.settings, wrong_output_connection)
    wrong_output_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: wrong_output_adapter,
    )
    assert wrong_output_result.status == "failed"
    assert wrong_output_result.failure_code == "schema_changed"
    assert wrong_output_result.rows == ()

    wrong_output_type_connection = _Connection(
        live_columns={"id": _live_column(catalog.columns["id"])},
        attributes=[_attribute("__nv_p0", "text")],
        records=[],
    )
    wrong_output_type_adapter, _calls = _adapter_with_connection(
        catalog.settings, wrong_output_type_connection
    )
    wrong_output_type_result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: wrong_output_type_adapter,
    )
    assert wrong_output_type_result.status == "failed"
    assert wrong_output_type_result.failure_code == "schema_changed"
    assert wrong_output_type_result.rows == ()


async def test_timeout_is_not_retried_and_is_recorded_safely(execution_catalog):
    catalog = execution_catalog

    class _TimeoutAdapter:
        calls = 0

        async def execute(self, **_kwargs):
            self.calls += 1
            raise PostgresExecutionTimeoutError()

    adapter = _TimeoutAdapter()
    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert adapter.calls == 1
    assert result.status == "failed"
    assert result.failure_code == "execution_timeout"
    assert result.rows == ()
    async with catalog.factory() as session:
        run = await session.get(PostgresQueryRun, result.execution_id)
    assert run is not None
    assert run.status is PostgresQueryRunStatus.FAILED
    assert run.failure_code == "execution_timeout"


async def test_operation_timeout_terminates_client_connection_without_retry(execution_catalog):
    catalog = execution_catalog
    timeout_settings = catalog.settings.model_copy(
        update={"postgres_execution_operation_timeout_seconds": 0.1}
    )
    connection = _Connection(
        live_columns={name: _live_column(column) for name, column in catalog.columns.items()},
        attributes=[_attribute("__nv_p0", "int8")],
        records=[_record(["__nv_p0"], [1])],
        block_cursor=True,
    )
    adapter, connect_calls = _adapter_with_connection(timeout_settings, connection)

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=timeout_settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.status == "failed"
    assert result.failure_code == "execution_timeout"
    assert result.rows == ()
    assert len(connect_calls) == 1
    assert connection.terminated is True
    assert connection.closed is True


async def test_raw_driver_detail_never_enters_the_typed_result_or_run(execution_catalog):
    catalog = execution_catalog
    unsafe_detail = "postgres://reader:password@private-host/analytics SELECT secret"

    class _UnsafeAdapter:
        async def execute(self, **_kwargs):
            raise RuntimeError(unsafe_detail)

    result = await execute_postgres_query(
        catalog.factory,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=_plan(catalog, "id"),
        settings=catalog.settings,
        adapter_factory=lambda _settings: _UnsafeAdapter(),
    )

    assert result.status == "failed"
    assert result.failure_code == "execution_failed"
    assert unsafe_detail not in result.model_dump_json()
    assert unsafe_detail not in str(result.safe_provenance())
    async with catalog.factory() as session:
        run = await session.get(PostgresQueryRun, result.execution_id)
    assert run is not None
    assert unsafe_detail not in repr(run)


async def test_process_local_limiter_releases_slots_after_completion_and_cancellation():
    limiter = PostgresExecutionLimiter(global_limit=1, per_user_limit=1, per_source_limit=1)
    owner = uuid.uuid4()
    source = uuid.uuid4()
    first_entered = asyncio.Event()
    allow_first_exit = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with limiter.slot(owner_user_id=owner, source_id=source):
            first_entered.set()
            await allow_first_exit.wait()

    async def second():
        async with limiter.slot(owner_user_id=owner, source_id=source):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert second_entered.is_set() is False
    allow_first_exit.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=1)
    assert second_entered.is_set() is True

    # A task cancelled while holding the slot also releases it.
    entered_again = asyncio.Event()

    async def blocked():
        async with limiter.slot(owner_user_id=owner, source_id=source):
            entered_again.set()
            await asyncio.Event().wait()

    blocked_task = asyncio.create_task(blocked())
    await asyncio.wait_for(entered_again.wait(), timeout=1)
    blocked_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_task
    async with limiter.slot(owner_user_id=owner, source_id=source):
        pass
