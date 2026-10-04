"""Focused Stage 5A confirmed metric definition boundary tests.

SQLite is used only for durable catalog/metric state.  These cases exercise
the owner and catalog boundary, never a live PostgreSQL data connection.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
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
    CatalogSnapshot,
    CatalogSyncRun,
    SourceCatalogHead,
)
from app.db.models.conversation import ConversationSessionRow
from app.db.models.dataset import DatasetRecord
from app.db.models.metric_definition import MetricDefinition, MetricDefinitionVersion
from app.db.models.source import (
    DataSource,
    SourceStatus,
    SourceTableScope,
    SourceType,
    TLSMode,
)
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.schemas.source_schemas import (
    MetricDefinitionCreateRequest,
    MetricDefinitionVersionCreateRequest,
)
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogColumnInput,
    CatalogSnapshotInput,
    CatalogTableIdentity,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    request_catalog_sync_for_owner,
)
from app.services.metric_service import (
    MetricConflictError,
    MetricNeedsReviewError,
    MetricRetiredError,
    create_metric_definition_for_owner,
    create_metric_definition_version_for_owner,
    expand_metric_query_intent,
    get_metric_definition_for_owner,
    list_metric_definition_versions_for_owner,
    list_metric_definitions_for_owner,
    resolve_metric_definition_for_owner,
    retire_metric_definition_for_owner,
)


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
    MetricDefinition.__table__,
    MetricDefinitionVersion.__table__,
]


@dataclass(frozen=True)
class _MetricCatalog:
    session: AsyncSession
    owner_id: uuid.UUID
    other_owner_id: uuid.UUID
    source_id: uuid.UUID
    identity: CatalogTableIdentity
    snapshot: CatalogSnapshot
    asset: CatalogAsset
    columns: dict[str, CatalogColumn]


@pytest.fixture
async def metric_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "api_key": "stage5a-metric-test-key",
        "database_url": "sqlite+aiosqlite://",
    }
    values.update(overrides)
    return Settings(**values)


async def _user(session: AsyncSession, email: str) -> User:
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


async def _source(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    identity: CatalogTableIdentity,
) -> DataSource:
    source = DataSource(
        id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        source_type=SourceType.POSTGRESQL,
        display_name="Metric service synthetic source",
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
    session.add(
        SourceTableScope(
            source_id=source.id,
            schema_name=identity.schema_name,
            table_name=identity.table_name,
        )
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
        column_comment=None,
    )


def _snapshot_input(
    identity: CatalogTableIdentity,
    *,
    amount_type: str = "numeric",
    amount_oid: int = 1700,
    amount_formatted: str = "numeric(12,2)",
) -> CatalogSnapshotInput:
    return CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=identity,
                table_comment=None,
                columns=(
                    _column("amount", 1, native_type_name=amount_type, native_type_oid=amount_oid, formatted_native_type=amount_formatted),
                    _column("status", 2, native_type_name="text", native_type_oid=25, formatted_native_type="text"),
                    _column("event_at", 3, native_type_name="timestamptz", native_type_oid=1184, formatted_native_type="timestamp with time zone"),
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
        session, source_id=source_id, run_id=run.id, snapshot_input=snapshot_input
    )


async def _current_catalog(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot: CatalogSnapshot,
) -> tuple[CatalogAsset, dict[str, CatalogColumn]]:
    asset = (
        await session.execute(
            select(CatalogAsset).where(
                CatalogAsset.source_id == source_id,
                CatalogAsset.snapshot_id == snapshot.id,
            )
        )
    ).scalar_one()
    columns = list(
        (
            await session.execute(
                select(CatalogColumn)
                .where(
                    CatalogColumn.source_id == source_id,
                    CatalogColumn.snapshot_id == snapshot.id,
                    CatalogColumn.asset_id == asset.id,
                )
                .order_by(CatalogColumn.ordinal_position)
            )
        ).scalars()
    )
    return asset, {column.column_name: column for column in columns}


@pytest.fixture
async def metric_catalog(metric_session: AsyncSession) -> _MetricCatalog:
    owner = await _user(metric_session, "metric-owner@example.test")
    other = await _user(metric_session, "metric-other@example.test")
    identity = CatalogTableIdentity("public", "orders")
    source = await _source(metric_session, owner_user_id=owner.id, identity=identity)
    snapshot = await _publish(
        metric_session,
        source_id=source.id,
        owner_user_id=owner.id,
        snapshot_input=_snapshot_input(identity),
    )
    asset, columns = await _current_catalog(metric_session, source_id=source.id, snapshot=snapshot)
    return _MetricCatalog(
        session=metric_session,
        owner_id=owner.id,
        other_owner_id=other.id,
        source_id=source.id,
        identity=identity,
        snapshot=snapshot,
        asset=asset,
        columns=columns,
    )


def _request(
    catalog: _MetricCatalog,
    *,
    name: str = "Paid order revenue",
    description: str | None = "Revenue from paid orders",
    aggregation: dict[str, object] | None = None,
    owner_confirmed: bool = True,
    fixed_filters: list[dict[str, object]] | None = None,
) -> MetricDefinitionCreateRequest:
    return MetricDefinitionCreateRequest.model_validate(
        {
            "name": name,
            "owner_confirmed": owner_confirmed,
            "description": description,
            "snapshot_id": catalog.snapshot.id,
            "asset_id": catalog.asset.id,
            "aggregation": aggregation or {"operation": "sum", "column_id": catalog.columns["amount"].id},
            "fixed_filters": fixed_filters
            if fixed_filters is not None
            else [
                {
                    "column_id": catalog.columns["status"].id,
                    "operator": "eq",
                    "value": {"kind": "text", "value": "paid"},
                }
            ],
            "unit_label": "USD",
            "default_date_column_id": catalog.columns["event_at"].id,
            "default_time_zone": "UTC",
        }
    )


async def test_confirmed_metric_is_versioned_normalized_and_expands_fixed_filters(metric_catalog: _MetricCatalog):
    with pytest.raises(ValidationError):
        _request(metric_catalog, owner_confirmed=False)

    first = await create_metric_definition_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        owner_user_id=metric_catalog.owner_id,
        request=_request(metric_catalog, name="  Paid   Order Revenue  "),
        settings=_settings(),
    )
    assert first.version_number == 1
    assert first.state == "valid"
    assert first.aggregation_operation == "sum"
    assert first.fixed_filters[0].column_id == metric_catalog.columns["status"].id

    with pytest.raises(MetricConflictError):
        await create_metric_definition_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            owner_user_id=metric_catalog.owner_id,
            request=_request(metric_catalog, name="paid order revenue"),
            settings=_settings(),
        )

    second_request = MetricDefinitionVersionCreateRequest.model_validate(
        _request(
            metric_catalog,
            name="Paid order revenue",
            description="Count of non-null paid order amounts",
            aggregation={"operation": "count", "column_id": metric_catalog.columns["amount"].id},
        ).model_dump()
    )
    second = await create_metric_definition_version_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        metric_id=first.metric_id,
        owner_user_id=metric_catalog.owner_id,
        request=second_request,
        settings=_settings(),
    )
    assert second.version_number == 2
    assert second.aggregation_operation == "count"
    assert second.aggregation_column_id == metric_catalog.columns["amount"].id

    history = await list_metric_definition_versions_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        metric_id=first.metric_id,
        owner_user_id=metric_catalog.owner_id,
    )
    assert [(item.version_number, item.description) for item in history] == [
        (2, "Count of non-null paid order amounts"),
        (1, "Revenue from paid orders"),
    ]

    resolved = await resolve_metric_definition_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        metric_id=first.metric_id,
        owner_user_id=metric_catalog.owner_id,
        settings=_settings(),
    )
    assert resolved.aggregation == "count_column"
    plan = expand_metric_query_intent(
        resolved,
        SimpleNamespace(
            metric_id=resolved.metric_id,
            version_number=resolved.version_number,
            additional_filters=(),
            group_by=(),
            sort=None,
            limit=20,
        ),
        _settings(),
    )
    assert plan.filters[0].column_id == metric_catalog.columns["status"].id
    assert plan.group_by == ()
    temporal_plan = expand_metric_query_intent(
        resolved,
        SimpleNamespace(
            metric_id=resolved.metric_id,
            version_number=resolved.version_number,
            additional_filters=(),
            group_by=(
                {
                    "column_id": metric_catalog.columns["event_at"].id,
                    "bucket": "day",
                    "time_zone": "UTC",
                },
            ),
            sort=None,
            limit=20,
        ),
        _settings(),
    )
    assert temporal_plan.group_by[0].column_id == metric_catalog.columns["event_at"].id
    assert temporal_plan.group_by[0].time_zone == "UTC"


async def test_compatible_refresh_rematerializes_and_incompatible_refresh_marks_review(metric_catalog: _MetricCatalog):
    created = await create_metric_definition_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        owner_user_id=metric_catalog.owner_id,
        request=_request(metric_catalog),
        settings=_settings(),
    )

    compatible_snapshot = await _publish(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        owner_user_id=metric_catalog.owner_id,
        snapshot_input=_snapshot_input(metric_catalog.identity),
    )
    compatible_asset, compatible_columns = await _current_catalog(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        snapshot=compatible_snapshot,
    )
    resolved = await resolve_metric_definition_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        metric_id=created.metric_id,
        owner_user_id=metric_catalog.owner_id,
        settings=_settings(),
    )
    assert resolved.snapshot_id == compatible_snapshot.id
    assert resolved.asset_id == compatible_asset.id
    assert resolved.aggregation_column_id == compatible_columns["amount"].id
    assert resolved.aggregation_column_id != metric_catalog.columns["amount"].id
    current_view = await get_metric_definition_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        metric_id=created.metric_id,
        owner_user_id=metric_catalog.owner_id,
        settings=_settings(),
    )
    assert current_view.snapshot_id == metric_catalog.snapshot.id  # immutable v1 provenance
    assert current_view.current_catalog is not None
    assert current_view.current_catalog.snapshot_id == compatible_snapshot.id
    assert current_view.current_catalog.asset_id == compatible_asset.id
    assert current_view.current_catalog.aggregation_column_id == compatible_columns["amount"].id

    await _publish(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        owner_user_id=metric_catalog.owner_id,
        snapshot_input=_snapshot_input(
            metric_catalog.identity,
            amount_type="text",
            amount_oid=25,
            amount_formatted="text",
        ),
    )
    listed = await list_metric_definitions_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        owner_user_id=metric_catalog.owner_id,
        settings=_settings(),
    )
    assert [(item.metric_id, item.state) for item in listed] == [(created.metric_id, "needs_review")]
    with pytest.raises(MetricNeedsReviewError):
        await resolve_metric_definition_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            metric_id=created.metric_id,
            owner_user_id=metric_catalog.owner_id,
            settings=_settings(),
        )


async def test_confirmation_rejects_wrong_current_column_types_and_filter_values(metric_catalog: _MetricCatalog):
    from app.services.metric_service import MetricValidationError

    with pytest.raises(MetricValidationError):
        await create_metric_definition_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            owner_user_id=metric_catalog.owner_id,
            request=_request(
                metric_catalog,
                aggregation={"operation": "sum", "column_id": metric_catalog.columns["status"].id},
            ),
            settings=_settings(),
        )
    with pytest.raises(MetricValidationError):
        await create_metric_definition_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            owner_user_id=metric_catalog.owner_id,
            request=_request(
                metric_catalog,
                fixed_filters=[
                    {
                        "column_id": metric_catalog.columns["status"].id,
                        "operator": "eq",
                        "value": {"kind": "integer", "value": 7},
                    }
                ],
            ),
            settings=_settings(),
        )


async def test_retirement_and_cross_owner_access_fail_closed(metric_catalog: _MetricCatalog):
    created = await create_metric_definition_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        owner_user_id=metric_catalog.owner_id,
        request=_request(metric_catalog, aggregation={"operation": "count"}),
        settings=_settings(),
    )
    with pytest.raises(NotFoundError):
        await get_metric_definition_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            metric_id=created.metric_id,
            owner_user_id=metric_catalog.other_owner_id,
            settings=_settings(),
        )
    with pytest.raises(NotFoundError):
        await list_metric_definitions_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            owner_user_id=metric_catalog.other_owner_id,
            settings=_settings(),
        )

    retired = await retire_metric_definition_for_owner(
        metric_catalog.session,
        source_id=metric_catalog.source_id,
        metric_id=created.metric_id,
        owner_user_id=metric_catalog.owner_id,
    )
    assert retired.state == "retired"
    assert retired.retired_at is not None
    with pytest.raises(MetricRetiredError):
        await resolve_metric_definition_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            metric_id=created.metric_id,
            owner_user_id=metric_catalog.owner_id,
            settings=_settings(),
        )
    assert len(
        await list_metric_definition_versions_for_owner(
            metric_catalog.session,
            source_id=metric_catalog.source_id,
            metric_id=created.metric_id,
            owner_user_id=metric_catalog.owner_id,
        )
    ) == 1
