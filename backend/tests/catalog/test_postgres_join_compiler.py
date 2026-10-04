"""Focused Stage 5B deterministic same-source join compilation tests.

SQLite stores only the application catalog and owner-confirmed relationship
contract.  These tests intentionally do not claim SQLite proves PostgreSQL
join, constraint, lock, or NULL semantics; the generated PostgreSQL text and
live-gateway expectations are covered independently.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import event, select
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
    CatalogForeignKeyTargetStatus,
    CatalogKeyKind,
    CatalogSnapshot,
    CatalogSyncRun,
    SourceCatalogHead,
)
from app.db.models.conversation import ConversationSessionRow
from app.db.models.dataset import DatasetRecord
from app.db.models.relationship_definition import (
    RelationshipDefinition,
    RelationshipDefinitionVersion,
)
from app.db.models.source import DataSource, SourceStatus, SourceTableScope, SourceType, TLSMode
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.schemas.source_schemas import RelationshipDefinitionCreateRequest
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogColumnInput,
    CatalogForeignKeyColumnInput,
    CatalogForeignKeyInput,
    CatalogKeyInput,
    CatalogSnapshotInput,
    CatalogTableIdentity,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    request_catalog_sync_for_owner,
)
from app.services.postgres_query_compiler import (
    PostgresQueryPlanValidationError,
    validate_and_compile_postgres_query,
)
from app.services.postgres_query_plan import PostgresProjection, PostgresQueryPlan
from app.services.relationship_service import create_relationship_definition_for_owner


_TABLES = [
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
    RelationshipDefinition.__table__,
    RelationshipDefinitionVersion.__table__,
]


@dataclass(frozen=True)
class _JoinCatalog:
    session: AsyncSession
    owner_id: uuid.UUID
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    orders_asset_id: uuid.UUID
    customers_asset_id: uuid.UUID
    relationship_id: uuid.UUID
    relationship_version_id: uuid.UUID
    # Store IDs rather than live ORM rows: expected non-enumerating failures
    # roll back the test session and expire ORM attributes.
    order_columns: dict[str, uuid.UUID]
    customer_columns: dict[str, uuid.UUID]


@pytest.fixture
async def join_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_TABLES)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _column(
    name: str,
    ordinal: int,
    *,
    native_type_name: str,
    native_type_oid: int,
    nullable: bool = False,
) -> CatalogColumnInput:
    return CatalogColumnInput(
        column_name=name,
        ordinal_position=ordinal,
        is_nullable=nullable,
        formatted_native_type=native_type_name,
        native_type_schema="pg_catalog",
        native_type_name=native_type_name,
        native_type_oid=native_type_oid,
        native_type_modifier=-1,
        column_comment=None,
    )


def _snapshot_input() -> CatalogSnapshotInput:
    customers = CatalogTableIdentity("sales", "customers")
    orders = CatalogTableIdentity("sales", "orders")
    tenant_customers = CatalogTableIdentity("sales", "tenant_customers")
    invoice_lines = CatalogTableIdentity("sales", "invoice_lines")
    return CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=customers,
                columns=(
                    _column("customer_id", 1, native_type_name="int8", native_type_oid=20),
                    _column("region", 2, native_type_name="text", native_type_oid=25, nullable=True),
                ),
                declared_keys=(
                    CatalogKeyInput(
                        constraint_name="customers_pkey",
                        key_kind=CatalogKeyKind.PRIMARY_KEY,
                        column_names=("customer_id",),
                    ),
                ),
            ),
            CatalogAssetInput(
                identity=orders,
                columns=(
                    _column("order_id", 1, native_type_name="int8", native_type_oid=20),
                    _column("customer_id", 2, native_type_name="int8", native_type_oid=20, nullable=True),
                    _column("amount", 3, native_type_name="numeric", native_type_oid=1700),
                    _column("status", 4, native_type_name="text", native_type_oid=25),
                ),
                declared_keys=(
                    CatalogKeyInput(
                        constraint_name="orders_pkey",
                        key_kind=CatalogKeyKind.PRIMARY_KEY,
                        column_names=("order_id",),
                    ),
                ),
                foreign_keys=(
                    CatalogForeignKeyInput(
                        constraint_name="orders_customer_fkey",
                        target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                        referenced_table=customers,
                        column_mappings=(
                            CatalogForeignKeyColumnInput(
                                ordinal_position=1,
                                referencing_column_name="customer_id",
                                referenced_column_name="customer_id",
                            ),
                        ),
                    ),
                ),
            ),
            CatalogAssetInput(
                identity=tenant_customers,
                columns=(
                    _column("tenant_id", 1, native_type_name="int8", native_type_oid=20),
                    _column("customer_id", 2, native_type_name="int8", native_type_oid=20),
                    _column("region", 3, native_type_name="text", native_type_oid=25),
                ),
                declared_keys=(
                    CatalogKeyInput(
                        constraint_name="tenant_customers_pkey",
                        key_kind=CatalogKeyKind.PRIMARY_KEY,
                        column_names=("tenant_id", "customer_id"),
                    ),
                ),
            ),
            CatalogAssetInput(
                identity=invoice_lines,
                columns=(
                    _column("line_id", 1, native_type_name="int8", native_type_oid=20),
                    _column("tenant_id", 2, native_type_name="int8", native_type_oid=20),
                    _column("customer_id", 3, native_type_name="int8", native_type_oid=20),
                    _column("amount", 4, native_type_name="numeric", native_type_oid=1700),
                ),
                declared_keys=(
                    CatalogKeyInput(
                        constraint_name="invoice_lines_pkey",
                        key_kind=CatalogKeyKind.PRIMARY_KEY,
                        column_names=("line_id",),
                    ),
                ),
                foreign_keys=(
                    CatalogForeignKeyInput(
                        constraint_name="invoice_lines_tenant_customer_fkey",
                        target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                        referenced_table=tenant_customers,
                        column_mappings=(
                            CatalogForeignKeyColumnInput(
                                ordinal_position=1,
                                referencing_column_name="tenant_id",
                                referenced_column_name="tenant_id",
                            ),
                            CatalogForeignKeyColumnInput(
                                ordinal_position=2,
                                referencing_column_name="customer_id",
                                referenced_column_name="customer_id",
                            ),
                        ),
                    ),
                ),
            ),
        )
    )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage5b-join-compiler-test-key",
        database_url="sqlite+aiosqlite://",
    )


async def _make_catalog(
    session: AsyncSession,
    *,
    base_table_name: str = "orders",
    lookup_table_name: str = "customers",
) -> _JoinCatalog:
    owner = User(
        id=uuid.uuid4(),
        email="join-owner@example.test",
        name="join-owner",
        role=UserRole.USER,
        is_active=True,
        is_verified=True,
    )
    source = DataSource(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        source_type=SourceType.POSTGRESQL,
        display_name="join compiler source",
        host="warehouse.example.test",
        port=5432,
        database_name="analytics",
        tls_mode=TLSMode.VERIFY_FULL,
        has_custom_ca=False,
        status=SourceStatus.TESTED,
        profiling_enabled=False,
        catalog_sync_sequence=0,
        table_scopes=[
            SourceTableScope(schema_name="sales", table_name="orders"),
            SourceTableScope(schema_name="sales", table_name="customers"),
            SourceTableScope(schema_name="sales", table_name="invoice_lines"),
            SourceTableScope(schema_name="sales", table_name="tenant_customers"),
        ],
    )
    # Insert the owner first: this fixture uses explicit UUIDs rather than a
    # mapped object relationship, so SQLite cannot infer flush ordering from
    # the composite source-owner foreign key.
    session.add(owner)
    await session.commit()
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
        snapshot_input=_snapshot_input(),
    )
    assets = {
        (asset.schema_name, asset.table_name): asset
        for asset in (
            await session.execute(
                select(CatalogAsset).where(
                    CatalogAsset.source_id == source.id,
                    CatalogAsset.snapshot_id == snapshot.id,
                )
            )
        ).scalars()
    }
    orders = assets[("sales", base_table_name)]
    customers = assets[("sales", lookup_table_name)]
    foreign_key = (
        await session.execute(
            select(CatalogForeignKey).where(
                CatalogForeignKey.source_id == source.id,
                CatalogForeignKey.snapshot_id == snapshot.id,
                CatalogForeignKey.referencing_asset_id == orders.id,
            )
        )
    ).scalar_one()
    relationship = await create_relationship_definition_for_owner(
        session,
        source_id=source.id,
        owner_user_id=owner.id,
        request=RelationshipDefinitionCreateRequest(
            owner_confirmed=True,
            snapshot_id=snapshot.id,
            foreign_key_id=foreign_key.id,
            join_type="left",
        ),
    )
    version = (
        await session.execute(
            select(RelationshipDefinitionVersion).where(
                RelationshipDefinitionVersion.relationship_definition_id == relationship.relationship_id
            )
        )
    ).scalar_one()
    columns = (
        await session.execute(
            select(CatalogColumn).where(
                CatalogColumn.source_id == source.id,
                CatalogColumn.snapshot_id == snapshot.id,
            )
        )
    ).scalars().all()
    return _JoinCatalog(
        session=session,
        owner_id=owner.id,
        source_id=source.id,
        snapshot_id=snapshot.id,
        orders_asset_id=orders.id,
        customers_asset_id=customers.id,
        relationship_id=relationship.relationship_id,
        relationship_version_id=version.id,
        order_columns={column.column_name: column.id for column in columns if column.asset_id == orders.id},
        customer_columns={
            column.column_name: column.id for column in columns if column.asset_id == customers.id
        },
    )


@pytest.fixture
async def join_catalog(join_session: AsyncSession) -> _JoinCatalog:
    return await _make_catalog(join_session)


def _join_payload(catalog: _JoinCatalog) -> dict[str, str]:
    return {
        "relationship_id": str(catalog.relationship_id),
        "relationship_version_id": str(catalog.relationship_version_id),
        "lookup_asset_id": str(catalog.customers_asset_id),
        "join_type": "left",
    }


def _plan(catalog: _JoinCatalog, **overrides: object) -> PostgresQueryPlan:
    values: dict[str, object] = {
        "asset_id": catalog.orders_asset_id,
        "join": _join_payload(catalog),
        "projections": (
            {
                "asset_id": catalog.orders_asset_id,
                "column_id": next(iter(catalog.order_columns.values())),
            },
        ),
        "limit": 50,
    }
    values.update(overrides)
    return PostgresQueryPlan.model_validate(values)


async def _compile(catalog: _JoinCatalog, plan: PostgresQueryPlan):
    return await validate_and_compile_postgres_query(
        catalog.session,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        snapshot_id=catalog.snapshot_id,
        plan=plan,
        settings=_settings(),
    )


async def test_compiles_one_confirmed_directed_left_join_with_lookup_grouping(join_catalog):
    plan = _plan(
        join_catalog,
        projections=(),
        aggregations=(
            {
                "asset_id": join_catalog.orders_asset_id,
                "operation": "sum",
                "column_id": join_catalog.order_columns["amount"],
            },
        ),
        group_by=(
            {
                "asset_id": join_catalog.customers_asset_id,
                "column_id": join_catalog.customer_columns["region"],
            },
        ),
    )

    compiled = await _compile(join_catalog, plan)

    assert compiled.join is not None
    assert compiled.join.relationship_id == join_catalog.relationship_id
    assert compiled.join.relationship_version_id == join_catalog.relationship_version_id
    assert compiled.join.foreign_key_constraint_name == "orders_customer_fkey"
    assert compiled.join.foreign_key_base_column_names == ("customer_id",)
    assert compiled.join.foreign_key_lookup_column_names == ("customer_id",)
    assert compiled.join.target_key_constraint_name == "customers_pkey"
    assert compiled.sql == (
        'SELECT "l"."region" AS "__nv_g0", pg_catalog.sum("b"."amount") AS "__nv_a0"\n'
        'FROM "sales"."orders" AS "b"\n'
        'LEFT JOIN "sales"."customers" AS "l" ON ("b"."customer_id" = "l"."customer_id")\n'
        'GROUP BY 1\n'
        'LIMIT $1::pg_catalog.int8'
    )
    assert compiled.semantics.left_join_preserves_unmatched_base_rows is True
    assert compiled.expected_output_columns[0].source_asset_id == join_catalog.customers_asset_id
    assert compiled.expected_output_columns[1].source_asset_id == join_catalog.orders_asset_id


async def test_compiles_ordered_composite_equality_mapping(join_session):
    catalog = await _make_catalog(
        join_session,
        base_table_name="invoice_lines",
        lookup_table_name="tenant_customers",
    )
    plan = _plan(
        catalog,
        projections=(),
        aggregations=(
            {
                "asset_id": catalog.orders_asset_id,
                "operation": "sum",
                "column_id": catalog.order_columns["amount"],
            },
        ),
        group_by=(
            {
                "asset_id": catalog.customers_asset_id,
                "column_id": catalog.customer_columns["region"],
            },
        ),
    )

    compiled = await _compile(catalog, plan)

    assert compiled.join is not None
    assert compiled.join.foreign_key_constraint_name == "invoice_lines_tenant_customer_fkey"
    assert compiled.join.foreign_key_base_column_names == ("tenant_id", "customer_id")
    assert compiled.join.foreign_key_lookup_column_names == ("tenant_id", "customer_id")
    assert compiled.join.target_key_constraint_name == "tenant_customers_pkey"
    assert compiled.sql == (
        'SELECT "l"."region" AS "__nv_g0", pg_catalog.sum("b"."amount") AS "__nv_a0"\n'
        'FROM "sales"."invoice_lines" AS "b"\n'
        'LEFT JOIN "sales"."tenant_customers" AS "l" ON '
        '("b"."tenant_id" = "l"."tenant_id") AND '
        '("b"."customer_id" = "l"."customer_id")\n'
        'GROUP BY 1\n'
        'LIMIT $1::pg_catalog.int8'
    )


async def test_left_lookup_filter_requires_explicit_on_or_where_and_compiles_on_without_loss(join_catalog):
    lookup_filter = {
        "asset_id": join_catalog.customers_asset_id,
        "column_id": join_catalog.customer_columns["region"],
        "operator": "eq",
        "placement": "join_on",
        "value": {"kind": "text", "value": "north"},
    }
    compiled = await _compile(join_catalog, _plan(join_catalog, filters=(lookup_filter,)))

    assert 'LEFT JOIN "sales"."customers" AS "l" ON ("b"."customer_id" = "l"."customer_id") AND ("l"."region" = $1::pg_catalog.text)' in compiled.sql
    assert "WHERE" not in compiled.sql
    assert compiled.semantics.lookup_filters_in_on is True
    assert compiled.parameters == ("north", 50)

    missing_placement = dict(lookup_filter)
    missing_placement.pop("placement")
    with pytest.raises(ValueError, match="LEFT JOIN lookup filters require explicit placement"):
        _plan(join_catalog, filters=(missing_placement,))


async def test_left_lookup_where_filter_explicitly_discards_unmatched_base_rows(join_catalog):
    compiled = await _compile(
        join_catalog,
        _plan(
            join_catalog,
            filters=(
                {
                    "asset_id": join_catalog.customers_asset_id,
                    "column_id": join_catalog.customer_columns["region"],
                    "operator": "eq",
                    "placement": "where",
                    "value": {"kind": "text", "value": "north"},
                },
            ),
        ),
    )

    assert 'WHERE ("l"."region" = $1::pg_catalog.text)' in compiled.sql
    assert compiled.semantics.lookup_filters_in_on is False
    assert compiled.semantics.lookup_filters_in_where is True
    assert compiled.semantics.left_join_preserves_unmatched_base_rows is False


async def test_left_lookup_is_null_where_filter_keeps_unmatched_base_rows(join_catalog):
    compiled = await _compile(
        join_catalog,
        _plan(
            join_catalog,
            filters=(
                {
                    "asset_id": join_catalog.customers_asset_id,
                    "column_id": join_catalog.customer_columns["region"],
                    "operator": "is_null",
                    "placement": "where",
                },
            ),
        ),
    )

    assert 'WHERE ("l"."region" IS NULL)' in compiled.sql
    assert compiled.semantics.lookup_filters_in_where is True
    assert compiled.semantics.left_join_preserves_unmatched_base_rows is True


async def test_join_rejects_unconfirmed_or_mismatched_relationship_identity(join_catalog):
    forged = _join_payload(join_catalog)
    forged["relationship_version_id"] = str(uuid.uuid4())
    with pytest.raises(NotFoundError):
        await _compile(join_catalog, _plan(join_catalog, join=forged))

    wrong_semantics = _join_payload(join_catalog)
    wrong_semantics["join_type"] = "inner"
    with pytest.raises(PostgresQueryPlanValidationError, match="does not match the typed join plan"):
        await _compile(join_catalog, _plan(join_catalog, join=wrong_semantics))


async def test_join_rejects_lookup_aggregation_to_preserve_base_metric_grain(join_catalog):
    plan = _plan(
        join_catalog,
        projections=(),
        aggregations=(
            {
                "asset_id": join_catalog.customers_asset_id,
                "operation": "count_column",
                "column_id": join_catalog.customer_columns["region"],
            },
        ),
    )

    with pytest.raises(PostgresQueryPlanValidationError, match="base asset"):
        await _compile(join_catalog, plan)


def test_join_plan_requires_qualified_columns_and_rejects_extra_tables(join_catalog):
    with pytest.raises(ValueError, match="require an asset_id"):
        _plan(
            join_catalog,
            projections=({"column_id": join_catalog.order_columns["order_id"]},),
        )
    with pytest.raises(ValueError, match="declared base or lookup"):
        _plan(
            join_catalog,
            projections=(
                {
                    "asset_id": uuid.uuid4(),
                    "column_id": join_catalog.order_columns["order_id"],
                },
        ),
    )


async def test_compiler_rechecks_join_qualification_after_internal_model_bypass(join_catalog):
    valid_plan = _plan(join_catalog)
    bypassed_plan = valid_plan.model_copy(
        update={
            "projections": (
                PostgresProjection.model_construct(
                    column_id=join_catalog.order_columns["order_id"],
                    asset_id=None,
                ),
            )
        }
    )

    with pytest.raises(PostgresQueryPlanValidationError, match="qualified base or lookup"):
        await _compile(join_catalog, bypassed_plan)
