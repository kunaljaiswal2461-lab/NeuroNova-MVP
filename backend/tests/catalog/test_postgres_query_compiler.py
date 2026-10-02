"""Focused Stage 4A validation and PostgreSQL compilation regressions.

These tests deliberately use the application catalog in SQLite only for the
authorization and catalog-resolution boundary.  They never execute the
generated PostgreSQL text: SQLite cannot establish asyncpg bind, PostgreSQL
function, privilege, search-path, or timestamp semantics.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
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
from app.db.models.source import (
    DataSource,
    SourceStatus,
    SourceTableScope,
    SourceType,
    TLSMode,
)
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogColumnInput,
    CatalogQueryContextError,
    CatalogSnapshotInput,
    CatalogTableIdentity,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    request_catalog_sync_for_owner,
)
from app.services.postgres_query_compiler import (
    PostgresQueryPlanValidationError,
    validate_and_compile_postgres_query,
    validate_postgres_query_plan,
)
from app.services.postgres_query_plan import PostgresQueryPlan


_CATALOG_TABLES = [
    User.__table__,
    DatasetRecord.__table__,
    ConversationSessionRow.__table__,
    DataSource.__table__,
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
]

_WEIRD_SCHEMA = "a.b"
_WEIRD_TABLE = 'orders:"2026"'
_WEIRD_INTEGER_COLUMN = 'line"item'
_SAMPLE_UUID = uuid.UUID("7a82a13d-5e92-4a18-8f2d-5d5fe1b7bd65")


@dataclass(frozen=True)
class _QueryCatalog:
    session: AsyncSession
    owner: User
    other: User
    source: DataSource
    snapshot: CatalogSnapshot
    asset: CatalogAsset
    columns: dict[str, CatalogColumn]
    snapshot_input: CatalogSnapshotInput
    # Catalog services intentionally roll back before non-enumerating errors.
    # Keep primitive IDs for assertions after such a rollback, rather than
    # accidentally triggering async lazy loads from expired ORM rows.
    owner_id: uuid.UUID
    other_id: uuid.UUID
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    integer_column_id: uuid.UUID


@pytest.fixture
async def postgres_query_session():
    """A real application-catalog session, with SQLite used only as storage."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=_CATALOG_TABLES
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _make_user(session: AsyncSession, email: str) -> User:
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


async def _make_source(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    scope: tuple[CatalogTableIdentity, ...],
) -> DataSource:
    source = DataSource(
        id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        source_type=SourceType.POSTGRESQL,
        display_name="Stage 4A synthetic source",
        host="warehouse.example.test",
        port=5432,
        database_name="analytics",
        tls_mode=TLSMode.VERIFY_FULL,
        has_custom_ca=False,
        status=SourceStatus.TESTED,
        profiling_enabled=False,
        catalog_sync_sequence=0,
    )
    session.add(source)
    session.add_all(
        [
            SourceTableScope(
                source_id=source.id,
                schema_name=identity.schema_name,
                table_name=identity.table_name,
            )
            for identity in scope
        ]
    )
    await session.commit()
    return source


def _column(
    name: str,
    ordinal: int,
    *,
    native_type_name: str,
    native_type_oid: int,
    formatted_native_type: str,
    nullable: bool = False,
) -> CatalogColumnInput:
    return CatalogColumnInput(
        column_name=name,
        ordinal_position=ordinal,
        is_nullable=nullable,
        formatted_native_type=formatted_native_type,
        native_type_schema="pg_catalog",
        native_type_name=native_type_name,
        native_type_oid=native_type_oid,
        native_type_modifier=-1,
        column_comment=f"Untrusted database comment for {name}",
    )


def _snapshot_input(identity: CatalogTableIdentity) -> CatalogSnapshotInput:
    """One selected asset containing the Stage 4A supported-type matrix."""

    return CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=identity,
                table_comment="<img src=x onerror=alert(1)> database description",
                columns=(
                    _column(
                        _WEIRD_INTEGER_COLUMN,
                        1,
                        native_type_name="int4",
                        native_type_oid=23,
                        formatted_native_type="integer",
                    ),
                    _column(
                        "amount",
                        2,
                        native_type_name="numeric",
                        native_type_oid=1700,
                        formatted_native_type="numeric(12,2)",
                    ),
                    _column(
                        "label",
                        3,
                        native_type_name="text",
                        native_type_oid=25,
                        formatted_native_type="text",
                        nullable=True,
                    ),
                    _column(
                        "order_date",
                        4,
                        native_type_name="date",
                        native_type_oid=1082,
                        formatted_native_type="date",
                    ),
                    _column(
                        "created_at",
                        5,
                        native_type_name="timestamp",
                        native_type_oid=1114,
                        formatted_native_type="timestamp without time zone",
                    ),
                    _column(
                        "event_at",
                        6,
                        native_type_name="timestamptz",
                        native_type_oid=1184,
                        formatted_native_type="timestamp with time zone",
                    ),
                    _column(
                        "is_active",
                        7,
                        native_type_name="bool",
                        native_type_oid=16,
                        formatted_native_type="boolean",
                    ),
                    _column(
                        "external_id",
                        8,
                        native_type_name="uuid",
                        native_type_oid=2950,
                        formatted_native_type="uuid",
                    ),
                    _column(
                        "payload",
                        9,
                        native_type_name="jsonb",
                        native_type_oid=3802,
                        formatted_native_type="jsonb",
                    ),
                ),
            ),
        )
    )


async def _publish(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    snapshot_input: CatalogSnapshotInput,
) -> CatalogSnapshot:
    run = await request_catalog_sync_for_owner(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    await mark_catalog_sync_running(session, source_id=source_id, run_id=run.id)
    return await publish_catalog_snapshot(
        session,
        source_id=source_id,
        run_id=run.id,
        snapshot_input=snapshot_input,
    )


@pytest.fixture
async def query_catalog(postgres_query_session: AsyncSession) -> _QueryCatalog:
    owner = await _make_user(postgres_query_session, "owner@example.test")
    other = await _make_user(postgres_query_session, "other@example.test")
    identity = CatalogTableIdentity(_WEIRD_SCHEMA, _WEIRD_TABLE)
    source = await _make_source(
        postgres_query_session,
        owner_user_id=owner.id,
        scope=(identity,),
    )
    snapshot_input = _snapshot_input(identity)
    snapshot = await _publish(
        postgres_query_session,
        source_id=source.id,
        owner_user_id=owner.id,
        snapshot_input=snapshot_input,
    )
    asset = (
        await postgres_query_session.execute(
            select(CatalogAsset).where(
                CatalogAsset.source_id == source.id,
                CatalogAsset.snapshot_id == snapshot.id,
            )
        )
    ).scalar_one()
    catalog_columns = list(
        (
            await postgres_query_session.execute(
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
    return _QueryCatalog(
        session=postgres_query_session,
        owner=owner,
        other=other,
        source=source,
        snapshot=snapshot,
        asset=asset,
        columns={column.column_name: column for column in catalog_columns},
        snapshot_input=snapshot_input,
        owner_id=owner.id,
        other_id=other.id,
        source_id=source.id,
        snapshot_id=snapshot.id,
        asset_id=asset.id,
        integer_column_id=next(
            column.id
            for column in catalog_columns
            if column.column_name == _WEIRD_INTEGER_COLUMN
        ),
    )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "api_key": "stage4a-query-plan-test-key",
        "database_url": "sqlite+aiosqlite://",
    }
    values.update(overrides)
    return Settings(**values)


def _plan(query_catalog: _QueryCatalog, **overrides: object) -> PostgresQueryPlan:
    values: dict[str, object] = {
        "asset_id": query_catalog.asset_id,
        "projections": [{"column_id": query_catalog.integer_column_id}],
        "limit": 25,
    }
    values.update(overrides)
    return PostgresQueryPlan.model_validate(values)


async def _validate(
    query_catalog: _QueryCatalog,
    plan: PostgresQueryPlan,
    *,
    owner_user_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    settings: Settings | None = None,
):
    return await validate_postgres_query_plan(
        query_catalog.session,
        owner_user_id=(
            query_catalog.owner_id if owner_user_id is None else owner_user_id
        ),
        source_id=query_catalog.source_id if source_id is None else source_id,
        snapshot_id=query_catalog.snapshot_id if snapshot_id is None else snapshot_id,
        plan=plan,
        settings=settings or _settings(),
    )


async def _compile(
    query_catalog: _QueryCatalog,
    plan: PostgresQueryPlan,
    *,
    owner_user_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    settings: Settings | None = None,
):
    return await validate_and_compile_postgres_query(
        query_catalog.session,
        owner_user_id=(
            query_catalog.owner_id if owner_user_id is None else owner_user_id
        ),
        source_id=query_catalog.source_id if source_id is None else source_id,
        snapshot_id=query_catalog.snapshot_id if snapshot_id is None else snapshot_id,
        plan=plan,
        settings=settings or _settings(),
    )


async def test_owner_can_validate_and_compile_one_current_scoped_asset(query_catalog):
    integer = query_catalog.columns[_WEIRD_INTEGER_COLUMN]
    plan = _plan(
        query_catalog,
        filters=(
            {
                "column_id": integer.id,
                "operator": "gte",
                "value": {"kind": "integer", "value": 7},
            },
        ),
        sort={"output_kind": "projection", "output_index": 0, "direction": "asc"},
    )

    validated = await _validate(query_catalog, plan)
    compiled = await _compile(query_catalog, plan)

    assert validated.source_id == query_catalog.source_id
    assert validated.snapshot_id == query_catalog.snapshot_id
    assert validated.asset_id == query_catalog.asset_id
    assert validated.columns[integer.id].name == _WEIRD_INTEGER_COLUMN
    assert compiled.parameters == (7, 25)
    assert compiled.sql == (
        'SELECT "t"."line""item" AS "__nv_p0"\n'
        'FROM "a.b"."orders:""2026""" AS "t"\n'
        'WHERE ("t"."line""item" >= $1::pg_catalog.int4)\n'
        'ORDER BY "__nv_p0" ASC\n'
        'LIMIT $2::pg_catalog.int8'
    )
    assert compiled.effective_limit == 25
    assert compiled.ordering[0].alias == "__nv_p0"
    assert compiled.ordering[0].direction == "asc"
    assert compiled.expected_output_columns[0].source_column_id == integer.id
    assert compiled.expected_output_columns[0].source_column_name == _WEIRD_INTEGER_COLUMN
    assert compiled.expected_output_columns[0].logical_type == "integer"
    assert compiled.semantics.result_order == "explicit_top_n"


async def test_other_owner_cross_source_historical_head_and_removed_scope_fail_closed(query_catalog):
    plan = _plan(query_catalog)

    with pytest.raises(NotFoundError):
        await _validate(query_catalog, plan, owner_user_id=query_catalog.other_id)

    second_identity = CatalogTableIdentity("other", "events")
    second_source = await _make_source(
        query_catalog.session,
        owner_user_id=query_catalog.owner_id,
        scope=(second_identity,),
    )
    second_snapshot = await _publish(
        query_catalog.session,
        source_id=second_source.id,
        owner_user_id=query_catalog.owner_id,
        snapshot_input=_snapshot_input(second_identity),
    )
    second_source_id = second_source.id
    second_snapshot_id = second_snapshot.id
    # A snapshot is scoped to its source; an otherwise owned snapshot ID is
    # not a transferable capability across sources.
    with pytest.raises(NotFoundError):
        await _validate(query_catalog, plan, snapshot_id=second_snapshot_id)
    with pytest.raises(NotFoundError):
        await _validate(
            query_catalog,
            plan,
            source_id=second_source_id,
            snapshot_id=second_snapshot_id,
        )

    # A newer publication makes the original immutable snapshot historical.
    await _publish(
        query_catalog.session,
        source_id=query_catalog.source_id,
        owner_user_id=query_catalog.owner_id,
        snapshot_input=query_catalog.snapshot_input,
    )
    with pytest.raises(CatalogQueryContextError):
        await _validate(query_catalog, plan)


async def test_removed_current_scope_cannot_be_recovered_from_an_immutable_snapshot(query_catalog):
    await query_catalog.session.execute(
        delete(SourceTableScope).where(SourceTableScope.source_id == query_catalog.source_id)
    )
    await query_catalog.session.commit()

    with pytest.raises(CatalogQueryContextError):
        await _validate(query_catalog, _plan(query_catalog))


async def test_unknown_or_cross_asset_column_references_fail_before_compilation(query_catalog):
    unknown_column_plan = _plan(
        query_catalog,
        projections=({"column_id": uuid.uuid4()},),
    )
    with pytest.raises(NotFoundError):
        await _validate(query_catalog, unknown_column_plan)

    # A valid asset ID from another source cannot be used with this source's
    # snapshot even when the caller owns both sources.
    other_identity = CatalogTableIdentity("other", "events")
    other_source = await _make_source(
        query_catalog.session,
        owner_user_id=query_catalog.owner_id,
        scope=(other_identity,),
    )
    other_snapshot = await _publish(
        query_catalog.session,
        source_id=other_source.id,
        owner_user_id=query_catalog.owner_id,
        snapshot_input=_snapshot_input(other_identity),
    )
    other_asset = (
        await query_catalog.session.execute(
            select(CatalogAsset).where(
                CatalogAsset.source_id == other_source.id,
                CatalogAsset.snapshot_id == other_snapshot.id,
            )
        )
    ).scalar_one()
    cross_asset_plan = _plan(query_catalog, asset_id=other_asset.id)
    with pytest.raises(NotFoundError):
        await _validate(query_catalog, cross_asset_plan)


def test_plan_contract_forbids_raw_sql_extra_fields_and_invalid_null_shapes(query_catalog):
    integer_id = query_catalog.columns[_WEIRD_INTEGER_COLUMN].id
    base = {
        "asset_id": query_catalog.asset_id,
        "projections": [{"column_id": integer_id}],
        "limit": 10,
    }

    with pytest.raises(ValidationError, match="extra_forbidden"):
        PostgresQueryPlan.model_validate({**base, "sql": "SELECT * FROM customer_data"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PostgresQueryPlan.model_validate(
            {**base, "function": "pg_catalog.pg_sleep"}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PostgresQueryPlan.model_validate(
            {**base, "expression": "amount * 1.2"}
        )
    with pytest.raises(ValidationError, match="literal_error"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "projections": [],
                "aggregations": [
                    {"operation": "pg_catalog.sum", "column_id": integer_id}
                ],
            }
        )
    with pytest.raises(ValidationError, match="projections cannot be combined with aggregations"):
        PostgresQueryPlan.model_validate(
            {**base, "aggregations": [{"operation": "count_all"}]}
        )
    with pytest.raises(ValidationError, match="group_by requires at least one aggregation"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "group_by": [{"column_id": integer_id}],
            }
        )
    with pytest.raises(ValidationError, match="is_null does not accept values"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "filters": [
                    {
                        "column_id": integer_id,
                        "operator": "is_null",
                        "value": {"kind": "integer", "value": 1},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="requires exactly one typed value"):
        PostgresQueryPlan.model_validate(
            {**base, "filters": [{"column_id": integer_id, "operator": "eq"}]}
        )


def test_text_parameters_reject_nul_and_utf8_byte_overflow(query_catalog):
    label_id = query_catalog.columns["label"].id
    base = {
        "asset_id": query_catalog.asset_id,
        "projections": [{"column_id": label_id}],
        "limit": 10,
    }
    with pytest.raises(ValidationError, match="must not contain NUL bytes"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "filters": [
                    {
                        "column_id": label_id,
                        "operator": "eq",
                        "value": {"kind": "text", "value": "before\x00after"},
                    }
                ],
            }
        )
    # Character count alone is not a safe transport bound: each of these
    # characters occupies three UTF-8 bytes.
    with pytest.raises(ValidationError, match="must not exceed 4096 UTF-8 bytes"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "filters": [
                    {
                        "column_id": label_id,
                        "operator": "eq",
                        "value": {"kind": "text", "value": "€" * 1366},
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    ("column_name", "operator", "value", "parameter_type"),
    [
        (_WEIRD_INTEGER_COLUMN, "gte", {"kind": "integer", "value": 7}, int),
        ("amount", "lt", {"kind": "decimal", "value": "12.34"}, Decimal),
        ("label", "contains", {"kind": "text", "value": "north"}, str),
        ("order_date", "gte", {"kind": "date", "value": "2026-01-02"}, date),
        (
            "created_at",
            "lt",
            {"kind": "timestamp", "value": "2026-01-02T03:04:05"},
            datetime,
        ),
        (
            "event_at",
            "gte",
            {"kind": "timestamptz", "value": "2026-01-02T03:04:05+05:30"},
            datetime,
        ),
        ("is_active", "eq", {"kind": "boolean", "value": True}, bool),
        ("external_id", "eq", {"kind": "uuid", "value": str(_SAMPLE_UUID)}, uuid.UUID),
    ],
)
async def test_supported_typed_filters_compile_as_bound_asyncpg_parameters(
    query_catalog,
    column_name,
    operator,
    value,
    parameter_type,
):
    column = query_catalog.columns[column_name]
    plan = _plan(
        query_catalog,
        filters=({"column_id": column.id, "operator": operator, "value": value},),
    )

    compiled = await _compile(query_catalog, plan)

    assert isinstance(compiled.parameters[0], parameter_type)
    assert compiled.parameters[-1] == 25
    assert "$1::pg_catalog." in compiled.sql
    assert "WHERE" in compiled.sql


async def test_rejects_unsupported_catalog_types_operators_and_incompatible_literals(query_catalog):
    jsonb = query_catalog.columns["payload"]
    text = query_catalog.columns["label"]
    integer = query_catalog.columns[_WEIRD_INTEGER_COLUMN]

    with pytest.raises(PostgresQueryPlanValidationError, match="type is not supported"):
        await _validate(query_catalog, _plan(query_catalog, projections=({"column_id": jsonb.id},)))
    with pytest.raises(PostgresQueryPlanValidationError, match="lt is not supported"):
        await _validate(
            query_catalog,
            _plan(
                query_catalog,
                filters=(
                    {
                        "column_id": text.id,
                        "operator": "lt",
                        "value": {"kind": "text", "value": "north"},
                    },
                ),
            ),
        )
    with pytest.raises(PostgresQueryPlanValidationError, match="requires a integer typed value"):
        await _validate(
            query_catalog,
            _plan(
                query_catalog,
                filters=(
                    {
                        "column_id": integer.id,
                        "operator": "eq",
                        "value": {"kind": "text", "value": "7"},
                    },
                ),
            ),
        )
    with pytest.raises(PostgresQueryPlanValidationError, match="sum requires a numeric column"):
        await _validate(
            query_catalog,
            _plan(
                query_catalog,
                projections=(),
                aggregations=({"operation": "sum", "column_id": text.id},),
            ),
        )


async def test_null_count_and_membership_semantics_are_explicit(query_catalog):
    integer = query_catalog.columns[_WEIRD_INTEGER_COLUMN]
    amount = query_catalog.columns["amount"]
    label = query_catalog.columns["label"]
    order_date = query_catalog.columns["order_date"]
    event_at = query_catalog.columns["event_at"]

    null_plan = _plan(
        query_catalog,
        filters=({"column_id": label.id, "operator": "is_null"},),
    )
    null_compiled = await _compile(query_catalog, null_plan)
    assert '"t"."label" IS NULL' in null_compiled.sql
    assert null_compiled.parameters == (25,)

    count_all = await _compile(
        query_catalog,
        _plan(
            query_catalog,
            projections=(),
            aggregations=({"operation": "count_all"},),
            limit=10,
        ),
    )
    count_column = await _compile(
        query_catalog,
        _plan(
            query_catalog,
            projections=(),
            aggregations=({"operation": "count_column", "column_id": label.id},),
            limit=10,
        ),
    )
    assert 'pg_catalog.count(*) AS "__nv_a0"' in count_all.sql
    assert 'pg_catalog.count("t"."label") AS "__nv_a0"' in count_column.sql
    assert count_all.semantics.count_all_is_row_count is True
    assert count_all.semantics.count_column_excludes_nulls is False
    assert count_column.semantics.count_all_is_row_count is False
    assert count_column.semantics.count_column_excludes_nulls is True

    supported_aggregates = await _compile(
        query_catalog,
        _plan(
            query_catalog,
            projections=(),
            aggregations=(
                {"operation": "count_all"},
                {"operation": "count_column", "column_id": label.id},
                {"operation": "sum", "column_id": amount.id},
                {"operation": "average", "column_id": amount.id},
                {"operation": "minimum", "column_id": order_date.id},
                {"operation": "maximum", "column_id": event_at.id},
            ),
            limit=10,
        ),
    )
    assert [column.operation for column in supported_aggregates.expected_output_columns] == [
        "count_all",
        "count_column",
        "sum",
        "average",
        "minimum",
        "maximum",
    ]
    assert [column.postgres_type for column in supported_aggregates.expected_output_columns] == [
        "int8",
        "int8",
        "numeric",
        "numeric",
        "date",
        "timestamptz",
    ]
    for function in ("count(*)", "count(", "sum(", "avg(", "min(", "max("):
        assert f"pg_catalog.{function}" in supported_aggregates.sql

    membership = await _compile(
        query_catalog,
        _plan(
            query_catalog,
            filters=(
                {
                    "column_id": integer.id,
                    "operator": "in",
                    "values": (
                        {"kind": "integer", "value": 1},
                        {"kind": "integer", "value": 2},
                    ),
                },
            ),
        ),
    )
    assert "= ANY($1::pg_catalog.int4[])" in membership.sql
    assert membership.parameters == ([1, 2], 25)


async def test_grouping_sorting_and_time_bucket_rules(query_catalog):
    event_at = query_catalog.columns["event_at"]
    created_at = query_catalog.columns["created_at"]
    label = query_catalog.columns["label"]
    valid_plan = _plan(
        query_catalog,
        projections=(),
        aggregations=({"operation": "count_all"},),
        group_by=(
            {
                "column_id": event_at.id,
                "bucket": "day",
                "time_zone": "Asia/Kolkata",
            },
        ),
        sort={"output_kind": "grouping", "output_index": 0, "direction": "desc"},
        limit=10,
    )
    compiled = await _compile(query_catalog, valid_plan)
    assert "pg_catalog.timezone($1::pg_catalog.text" in compiled.sql
    assert "GROUP BY 1" in compiled.sql
    assert 'ORDER BY "__nv_g0" DESC' in compiled.sql
    assert compiled.parameters == ("Asia/Kolkata", 10)
    assert compiled.expected_output_columns[0].logical_type == "date_bucket"
    assert compiled.expected_output_columns[0].time_zone == "Asia/Kolkata"
    assert compiled.semantics.timestamp_bucket_notes == (
        "timestamptz bucket rendered as local timestamp in Asia/Kolkata",
    )

    utc_plan = _plan(
        query_catalog,
        projections=(),
        aggregations=({"operation": "count_all"},),
        group_by=(
            {
                "column_id": event_at.id,
                "bucket": "day",
                "time_zone": "UTC",
            },
        ),
        sort={"output_kind": "grouping", "output_index": 0, "direction": "desc"},
        limit=10,
    )
    utc_compiled = await _compile(query_catalog, utc_plan)
    assert utc_compiled.parameters == ("UTC", 10)
    # The timezone changes the calculation even though filter parameters do
    # not enter a safe fingerprint.
    assert utc_compiled.fingerprint != compiled.fingerprint

    missing_zone = _plan(
        query_catalog,
        projections=(),
        aggregations=({"operation": "count_all"},),
        group_by=({"column_id": event_at.id, "bucket": "day"},),
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="require an explicit IANA timezone"):
        await _validate(query_catalog, missing_zone)

    timezone_for_naive = _plan(
        query_catalog,
        projections=(),
        aggregations=({"operation": "count_all"},),
        group_by=(
            {
                "column_id": created_at.id,
                "bucket": "day",
                "time_zone": "UTC",
            },
        ),
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="only supported for timestamptz"):
        await _validate(query_catalog, timezone_for_naive)

    text_bucket = _plan(
        query_catalog,
        projections=(),
        aggregations=({"operation": "count_all"},),
        group_by=({"column_id": label.id, "bucket": "month"},),
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="time bucket is not supported"):
        await _validate(query_catalog, text_bucket)

    invalid_aggregate_sort = _plan(
        query_catalog,
        projections=(),
        aggregations=({"operation": "count_all"},),
        sort={"output_kind": "projection", "output_index": 0, "direction": "asc"},
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="aggregate plans cannot sort"):
        await _validate(query_catalog, invalid_aggregate_sort)

    invalid_projection_sort = _plan(
        query_catalog,
        sort={"output_kind": "grouping", "output_index": 0, "direction": "asc"},
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="non-aggregate plans can sort"):
        await _validate(query_catalog, invalid_projection_sort)

    out_of_range_sort = _plan(
        query_catalog,
        projections=(),
        aggregations=({"operation": "count_all"},),
        sort={"output_kind": "aggregation", "output_index": 1, "direction": "asc"},
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="sort must reference"):
        await _validate(query_catalog, out_of_range_sort)


def test_date_timestamp_and_timestamptz_literals_have_distinct_boundary_rules(query_catalog):
    base = {
        "asset_id": query_catalog.asset_id,
        "projections": [{"column_id": query_catalog.columns[_WEIRD_INTEGER_COLUMN].id}],
        "limit": 10,
    }
    with pytest.raises(ValidationError, match="date values"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "filters": [
                    {
                        "column_id": query_catalog.columns["order_date"].id,
                        "operator": "eq",
                        "value": {"kind": "date", "value": "2026-01-02T00:00:00"},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="timestamp values must not include a timezone"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "filters": [
                    {
                        "column_id": query_catalog.columns["created_at"].id,
                        "operator": "eq",
                        "value": {
                            "kind": "timestamp",
                            "value": "2026-01-02T03:04:05+05:30",
                        },
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="timestamptz values must include a timezone"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "filters": [
                    {
                        "column_id": query_catalog.columns["event_at"].id,
                        "operator": "eq",
                        "value": {"kind": "timestamptz", "value": "2026-01-02T03:04:05"},
                    }
                ],
            }
        )
    # A date-only literal is not a timestamp boundary.  Accepting it would
    # silently manufacture midnight and blur date/range semantics.
    with pytest.raises(ValidationError, match="timestamp values must include a time"):
        PostgresQueryPlan.model_validate(
            {
                **base,
                "filters": [
                    {
                        "column_id": query_catalog.columns["created_at"].id,
                        "operator": "eq",
                        "value": {"kind": "timestamp", "value": "2026-01-02"},
                    }
                ],
            }
        )


async def test_complexity_limits_reject_excessive_projections_filters_in_values_and_limit(
    query_catalog,
):
    integer = query_catalog.columns[_WEIRD_INTEGER_COLUMN]
    amount = query_catalog.columns["amount"]

    with pytest.raises(PostgresQueryPlanValidationError, match="at most 1 projections"):
        await _validate(
            query_catalog,
            _plan(
                query_catalog,
                projections=(
                    {"column_id": integer.id},
                    {"column_id": amount.id},
                ),
            ),
            settings=_settings(postgres_query_max_projections=1),
        )

    two_filters = _plan(
        query_catalog,
        filters=(
            {
                "column_id": integer.id,
                "operator": "gte",
                "value": {"kind": "integer", "value": 1},
            },
            {
                "column_id": integer.id,
                "operator": "lte",
                "value": {"kind": "integer", "value": 2},
            },
        ),
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="at most 1 filters"):
        await _validate(
            query_catalog,
            two_filters,
            settings=_settings(postgres_query_max_filters=1),
        )

    two_membership_values = _plan(
        query_catalog,
        filters=(
            {
                "column_id": integer.id,
                "operator": "in",
                "values": (
                    {"kind": "integer", "value": 1},
                    {"kind": "integer", "value": 2},
                ),
            },
        ),
    )
    with pytest.raises(PostgresQueryPlanValidationError, match="must not exceed 1"):
        await _validate(
            query_catalog,
            two_membership_values,
            settings=_settings(postgres_query_max_in_values=1),
        )

    with pytest.raises(PostgresQueryPlanValidationError, match="limit must not exceed 500"):
        await _validate(query_catalog, _plan(query_catalog, limit=501))


async def test_total_bound_parameter_bytes_are_limited_before_compilation(query_catalog):
    label = query_catalog.columns["label"]
    plan = _plan(
        query_catalog,
        projections=({"column_id": label.id},),
        filters=(
            {
                "column_id": label.id,
                "operator": "eq",
                "value": {"kind": "text", "value": "x" * 1024},
            },
        ),
    )

    # The configured cap includes the compiled LIMIT bind, so the 1,024-byte
    # text alone is already too large for this deliberately small budget.
    with pytest.raises(PostgresQueryPlanValidationError, match="bound parameter data must not exceed 1024"):
        await _validate(
            query_catalog,
            plan,
            settings=_settings(postgres_query_max_parameter_bytes=1024),
        )


async def test_injection_like_values_remain_bound_and_never_enter_sql_repr_or_fingerprint(
    query_catalog,
):
    label = query_catalog.columns["label"]
    injection = "' OR 1=1; SELECT pg_sleep(10); --"
    first_plan = _plan(
        query_catalog,
        projections=({"column_id": label.id},),
        filters=(
            {
                "column_id": label.id,
                "operator": "contains",
                "value": {"kind": "text", "value": injection},
            },
        ),
    )
    second_plan = _plan(
        query_catalog,
        projections=({"column_id": label.id},),
        filters=(
            {
                "column_id": label.id,
                "operator": "contains",
                "value": {"kind": "text", "value": "benign"},
            },
        ),
    )

    first = await _compile(query_catalog, first_plan)
    repeated = await _compile(query_catalog, first_plan)
    second = await _compile(query_catalog, second_plan)

    assert first.parameters == (injection, 25)
    assert injection not in first.sql
    assert injection not in repr(first)
    assert injection not in str(first.safe_provenance())
    assert "pg_catalog.strpos" in first.sql
    assert "$1::pg_catalog.text" in first.sql
    assert first.sql == repeated.sql
    assert first.parameters == repeated.parameters
    assert first.fingerprint == repeated.fingerprint == second.fingerprint
    assert first.parameters != second.parameters

    different_limit = await _compile(query_catalog, _plan(
        query_catalog,
        projections=({"column_id": label.id},),
        filters=(
            {
                "column_id": label.id,
                "operator": "contains",
                "value": {"kind": "text", "value": injection},
            },
        ),
        limit=26,
    ))
    assert different_limit.parameters == (injection, 26)
    assert different_limit.fingerprint != first.fingerprint
