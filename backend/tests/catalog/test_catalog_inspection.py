"""Focused Stage 3C service tests for safe catalog browsing and comparison."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models.catalog import (
    CatalogForeignKeyTargetStatus,
    CatalogKeyKind,
)
from app.db.models.conversation import ConversationSessionRow
from app.db.models.dataset import DatasetRecord
from app.db.models.source import DataSource, SourceStatus, SourceTableScope, TLSMode
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogAssetSummary,
    CatalogColumnMetadata,
    CatalogColumnInput,
    CatalogForeignKeyColumnInput,
    CatalogForeignKeyInput,
    CatalogKeyInput,
    CatalogSnapshotInput,
    CatalogTableIdentity,
    _CatalogAssetState,
    _build_catalog_structural_diff,
    get_catalog_asset_detail_for_owner,
    get_catalog_structural_diff_for_owner,
    list_catalog_assets_for_owner,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    request_catalog_sync_for_owner,
)


_CATALOG_TABLES = [
    User.__table__,
    DatasetRecord.__table__,
    ConversationSessionRow.__table__,
    DataSource.__table__,
    SourceTableScope.__table__,
    Base.metadata.tables["catalog_sync_runs"],
    Base.metadata.tables["catalog_snapshots"],
    Base.metadata.tables["source_catalog_heads"],
    Base.metadata.tables["catalog_assets"],
    Base.metadata.tables["catalog_columns"],
    Base.metadata.tables["catalog_declared_keys"],
    Base.metadata.tables["catalog_declared_key_columns"],
    Base.metadata.tables["catalog_foreign_keys"],
    Base.metadata.tables["catalog_foreign_key_columns"],
]


@pytest.fixture
async def inspection_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_CATALOG_TABLES)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


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


async def _make_source(
    session,
    *,
    owner: User,
    scope: tuple[CatalogTableIdentity, ...] = (
        CatalogTableIdentity("sales", "customers"),
        CatalogTableIdentity("sales", "orders"),
    ),
) -> DataSource:
    source = DataSource(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        display_name="Inspection warehouse",
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
    native_type: str = "integer",
    nullable: bool = False,
    comment: str | None = None,
) -> CatalogColumnInput:
    return CatalogColumnInput(
        column_name=name,
        ordinal_position=ordinal,
        is_nullable=nullable,
        formatted_native_type=native_type,
        native_type_schema="pg_catalog",
        native_type_name="int4" if native_type == "integer" else native_type,
        native_type_oid=23,
        native_type_modifier=-1,
        column_comment=comment if comment is not None else f"{name} database comment",
    )


def _catalog_input(
    *,
    customer_comment: str = "Customer database comment",
    order_comment: str = "Order database comment",
    add_amount: bool = False,
    order_id_type: str = "integer",
    customer_id_nullable: bool = False,
    include_order_unique: bool = True,
    single_column_foreign_key: bool = False,
) -> CatalogSnapshotInput:
    customers = CatalogTableIdentity("sales", "customers")
    orders = CatalogTableIdentity("sales", "orders")
    order_columns = [
        _column("order_id", 1, native_type=order_id_type),
        _column("tenant_id", 2),
        _column(
            "customer_id",
            3,
            nullable=customer_id_nullable,
            comment="<img src=x onerror=alert(1)>",
        ),
    ]
    if add_amount:
        order_columns.append(_column("amount", 4, native_type="numeric"))
    order_keys = [
        CatalogKeyInput(
            constraint_name="orders_pkey",
            key_kind=CatalogKeyKind.PRIMARY_KEY,
            column_names=("order_id",),
        )
    ]
    if include_order_unique:
        order_keys.append(
            CatalogKeyInput(
                constraint_name="orders_customer_unique",
                key_kind=CatalogKeyKind.UNIQUE,
                column_names=("tenant_id", "customer_id"),
            )
        )
    return CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=customers,
                table_comment=customer_comment,
                columns=(
                    _column("tenant_id", 1),
                    _column("customer_id", 2),
                ),
                declared_keys=(
                    CatalogKeyInput(
                        constraint_name="customers_pkey",
                        key_kind=CatalogKeyKind.PRIMARY_KEY,
                        column_names=("tenant_id", "customer_id"),
                    ),
                ),
            ),
            CatalogAssetInput(
                identity=orders,
                table_comment=order_comment,
                columns=tuple(order_columns),
                declared_keys=tuple(order_keys),
                foreign_keys=(
                    CatalogForeignKeyInput(
                        constraint_name="orders_customer_fkey",
                        target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                        referenced_table=customers,
                        column_mappings=(
                            (CatalogForeignKeyColumnInput(1, "customer_id", "customer_id"),)
                            if single_column_foreign_key
                            else (
                                CatalogForeignKeyColumnInput(1, "tenant_id", "tenant_id"),
                                CatalogForeignKeyColumnInput(2, "customer_id", "customer_id"),
                            )
                        ),
                    ),
                    CatalogForeignKeyInput(
                        constraint_name="orders_unselected_target_fkey",
                        target_status=CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE,
                    ),
                ),
            ),
        )
    )


def _orders_only_input() -> CatalogSnapshotInput:
    return CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=CatalogTableIdentity("sales", "orders"),
                table_comment="Orders after scope change",
                columns=(
                    _column("order_id", 1),
                    _column("tenant_id", 2),
                    _column("customer_id", 3),
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
                        target_status=CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE,
                    ),
                ),
            ),
        )
    )


async def _publish(session, source: DataSource, owner: User, payload: CatalogSnapshotInput):
    run = await request_catalog_sync_for_owner(
        session, source_id=source.id, owner_user_id=owner.id
    )
    await mark_catalog_sync_running(session, source_id=source.id, run_id=run.id)
    return await publish_catalog_snapshot(
        session,
        source_id=source.id,
        run_id=run.id,
        snapshot_input=payload,
    )


async def test_pinned_asset_browse_and_detail_preserve_ordered_structural_metadata(
    inspection_session,
):
    owner = await _make_user(inspection_session, "owner@example.test")
    source = await _make_source(inspection_session, owner=owner)
    snapshot = await _publish(inspection_session, source, owner, _catalog_input())

    first_page = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=snapshot.id,
        owner_user_id=owner.id,
        page=1,
        page_size=1,
    )
    second_page = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=snapshot.id,
        owner_user_id=owner.id,
        page=2,
        page_size=1,
    )
    assert first_page.snapshot.id == snapshot.id
    assert first_page.scope_matches_current_selection is True
    assert first_page.total == 2
    assert [item.table_name for item in first_page.assets + second_page.assets] == [
        "customers",
        "orders",
    ]
    searched = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=snapshot.id,
        owner_user_id=owner.id,
        page=1,
        page_size=10,
        search="orders",
    )
    literal_wildcard = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=snapshot.id,
        owner_user_id=owner.id,
        page=1,
        page_size=10,
        search="ord_",
    )
    assert [item.table_name for item in searched.assets] == ["orders"]
    assert literal_wildcard.total == 0

    orders = second_page.assets[0]
    detail = await get_catalog_asset_detail_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=snapshot.id,
        asset_id=orders.asset_id,
        owner_user_id=owner.id,
    )
    assert [column.column_name for column in detail.columns] == [
        "order_id",
        "tenant_id",
        "customer_id",
    ]
    assert detail.columns[-1].column_comment == "<img src=x onerror=alert(1)>"
    assert [key.column_names for key in detail.declared_keys] == [
        ("tenant_id", "customer_id"),
        ("order_id",),
    ]
    visible_foreign_key, omitted_foreign_key = detail.foreign_keys
    assert visible_foreign_key.target_status is CatalogForeignKeyTargetStatus.IN_SCOPE
    assert visible_foreign_key.referenced_asset is not None
    assert visible_foreign_key.referenced_asset.table_name == "customers"
    assert [mapping.ordinal_position for mapping in visible_foreign_key.column_mappings] == [1, 2]
    assert omitted_foreign_key.target_status is CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE
    assert omitted_foreign_key.referenced_asset is None
    assert omitted_foreign_key.column_mappings == ()


async def test_historical_browse_filters_current_scope_and_redacts_old_foreign_key_target(
    inspection_session,
):
    owner = await _make_user(inspection_session, "owner@example.test")
    source = await _make_source(inspection_session, owner=owner)
    first_snapshot = await _publish(inspection_session, source, owner, _catalog_input())
    source_id = source.id
    owner_id = owner.id
    first_snapshot_id = first_snapshot.id
    first_assets = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=source_id,
        snapshot_id=first_snapshot_id,
        owner_user_id=owner_id,
        page=1,
        page_size=10,
    )
    orders = next(asset for asset in first_assets.assets if asset.table_name == "orders")
    customers = next(asset for asset in first_assets.assets if asset.table_name == "customers")

    await inspection_session.execute(
        delete(SourceTableScope).where(
            SourceTableScope.source_id == source_id,
            SourceTableScope.schema_name == "sales",
            SourceTableScope.table_name == "customers",
        )
    )
    await inspection_session.commit()
    await _publish(inspection_session, source, owner, _orders_only_input())

    historical_page = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=source_id,
        snapshot_id=first_snapshot_id,
        owner_user_id=owner_id,
        page=1,
        page_size=10,
    )
    assert historical_page.scope_matches_current_selection is False
    assert [asset.table_name for asset in historical_page.assets] == ["orders"]
    with pytest.raises(NotFoundError):
        await get_catalog_asset_detail_for_owner(
            inspection_session,
            source_id=source_id,
            snapshot_id=first_snapshot_id,
            asset_id=customers.asset_id,
            owner_user_id=owner_id,
        )
    detail = await get_catalog_asset_detail_for_owner(
        inspection_session,
        source_id=source_id,
        snapshot_id=first_snapshot_id,
        asset_id=orders.asset_id,
        owner_user_id=owner_id,
    )
    redacted = next(item for item in detail.foreign_keys if item.constraint_name == "orders_customer_fkey")
    assert redacted.target_status is CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE
    assert redacted.referenced_asset is None
    assert redacted.column_mappings == ()


async def test_structural_diff_is_baselined_then_reports_changes_without_rename_guessing(
    inspection_session,
):
    owner = await _make_user(inspection_session, "owner@example.test")
    source = await _make_source(inspection_session, owner=owner)
    first_snapshot = await _publish(
        inspection_session,
        source,
        owner,
        _catalog_input(customer_comment="Original customer comment"),
    )
    baseline = await get_catalog_structural_diff_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=first_snapshot.id,
        owner_user_id=owner.id,
    )
    assert baseline.state == "baseline"
    assert baseline.previous_snapshot is None

    second_snapshot = await _publish(
        inspection_session,
        source,
        owner,
        _catalog_input(
            customer_comment="Changed customer comment",
            add_amount=True,
            order_id_type="bigint",
            customer_id_nullable=True,
            include_order_unique=False,
            single_column_foreign_key=True,
        ),
    )
    diff = await get_catalog_structural_diff_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=second_snapshot.id,
        owner_user_id=owner.id,
    )
    assert diff.state == "compared"
    assert diff.previous_snapshot is not None and diff.previous_snapshot.id == first_snapshot.id
    assert [(item.table_name, item.column_name) for item in diff.columns_added] == [
        ("orders", "amount")
    ]
    assert [(item.table_name, item.column_name) for item in diff.columns_changed] == [
        ("orders", "customer_id"),
        ("orders", "order_id"),
    ]
    assert next(item for item in diff.columns_changed if item.column_name == "customer_id").current_is_nullable is True
    assert [(item.change, item.constraint_name) for item in diff.keys_changed] == [
        ("removed", "orders_customer_unique")
    ]
    assert [(item.change, item.constraint_name) for item in diff.foreign_keys_changed] == [
        ("added", "orders_customer_fkey"),
        ("removed", "orders_customer_fkey"),
    ]
    assert [(item.subject, item.table_name) for item in diff.comments_changed] == [
        ("table", "customers")
    ]
    # Identity is schema+table; no rename heuristic or table addition/removal
    # is claimed from a type/comment/key change.
    assert diff.tables_added == ()
    assert diff.tables_removed == ()


async def test_scope_change_returns_no_diff_details_and_never_claims_a_drop(inspection_session):
    owner = await _make_user(inspection_session, "owner@example.test")
    source = await _make_source(inspection_session, owner=owner)
    await _publish(inspection_session, source, owner, _catalog_input())
    await inspection_session.execute(
        delete(SourceTableScope).where(
            SourceTableScope.source_id == source.id,
            SourceTableScope.schema_name == "sales",
            SourceTableScope.table_name == "customers",
        )
    )
    await inspection_session.commit()
    second_snapshot = await _publish(inspection_session, source, owner, _orders_only_input())

    diff = await get_catalog_structural_diff_for_owner(
        inspection_session,
        source_id=source.id,
        snapshot_id=second_snapshot.id,
        owner_user_id=owner.id,
    )
    assert diff.state == "scope_changed"
    assert diff.previous_snapshot is None
    assert diff.tables_added == ()
    assert diff.tables_removed == ()
    assert diff.columns_added == ()
    assert diff.comments_changed == ()


async def test_catalog_inspection_requires_concrete_source_owner_identity(inspection_session):
    owner = await _make_user(inspection_session, "owner@example.test")
    other = await _make_user(inspection_session, "other@example.test")
    source = await _make_source(inspection_session, owner=owner)
    snapshot = await _publish(inspection_session, source, owner, _catalog_input())
    source_id = source.id
    snapshot_id = snapshot.id
    owner_id = owner.id
    other_id = other.id
    assets = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=source_id,
        snapshot_id=snapshot_id,
        owner_user_id=owner_id,
        page=1,
        page_size=10,
    )

    with pytest.raises(NotFoundError):
        await list_catalog_assets_for_owner(
            inspection_session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            owner_user_id=other_id,
            page=1,
            page_size=10,
        )
    with pytest.raises(NotFoundError):
        await get_catalog_asset_detail_for_owner(
            inspection_session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=assets.assets[0].asset_id,
            owner_user_id=None,  # type: ignore[arg-type]
        )
    with pytest.raises(NotFoundError):
        await get_catalog_structural_diff_for_owner(
            inspection_session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            owner_user_id=other_id,
        )


async def test_catalog_inspection_rejects_cross_source_snapshot_and_asset_ids(inspection_session):
    owner = await _make_user(inspection_session, "owner@example.test")
    first_source = await _make_source(inspection_session, owner=owner)
    second_source = await _make_source(
        inspection_session,
        owner=owner,
        scope=(CatalogTableIdentity("other", "events"),),
    )
    first_snapshot = await _publish(inspection_session, first_source, owner, _catalog_input())
    first_source_id = first_source.id
    first_snapshot_id = first_snapshot.id
    owner_id = owner.id
    second_payload = CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=CatalogTableIdentity("other", "events"),
                columns=(_column("event_id", 1),),
                declared_keys=(
                    CatalogKeyInput(
                        constraint_name="events_pkey",
                        key_kind=CatalogKeyKind.PRIMARY_KEY,
                        column_names=("event_id",),
                    ),
                ),
            ),
        )
    )
    second_snapshot = await _publish(inspection_session, second_source, owner, second_payload)
    first_assets = await list_catalog_assets_for_owner(
        inspection_session,
        source_id=first_source_id,
        snapshot_id=first_snapshot_id,
        owner_user_id=owner_id,
        page=1,
        page_size=10,
    )

    with pytest.raises(NotFoundError):
        await list_catalog_assets_for_owner(
            inspection_session,
            source_id=first_source_id,
            snapshot_id=second_snapshot.id,
            owner_user_id=owner_id,
            page=1,
            page_size=10,
        )
    with pytest.raises(NotFoundError):
        await get_catalog_asset_detail_for_owner(
            inspection_session,
            source_id=first_source_id,
            snapshot_id=first_snapshot_id,
            asset_id=uuid.uuid4(),
            owner_user_id=owner_id,
        )
    assert first_assets.assets


def test_structural_diff_global_change_limit_is_deterministic_and_signalled():
    source_id = uuid.uuid4()
    asset = CatalogAssetSummary(
        asset_id=uuid.uuid4(),
        schema_name="public",
        table_name="wide_table",
        table_comment=None,
    )
    current_columns = tuple(
        CatalogColumnMetadata(
            column_name=f"column_{position:03d}",
            ordinal_position=position,
            is_nullable=False,
            formatted_native_type="integer",
            native_type_schema="pg_catalog",
            native_type_name="int4",
            native_type_oid=23,
            native_type_modifier=-1,
            column_comment=None,
        )
        for position in range(1, 502)
    )
    previous = {
        ("public", "wide_table"): _CatalogAssetState(
            asset=asset,
            columns=(),
            declared_keys=(),
            foreign_keys=(),
        )
    }
    current = {
        ("public", "wide_table"): _CatalogAssetState(
            asset=asset,
            columns=current_columns,
            declared_keys=(),
            foreign_keys=(),
        )
    }
    diff = _build_catalog_structural_diff(
        source_id=source_id,
        snapshot=SimpleNamespace(id=uuid.uuid4(), published_at=None),
        previous_snapshot=SimpleNamespace(id=uuid.uuid4(), published_at=None),
        previous=previous,
        current=current,
    )

    assert diff.truncated is True
    assert diff.change_limit == 500
    assert len(diff.columns_added) == 500
    assert diff.columns_added[0].column_name == "column_001"
    assert diff.columns_added[-1].column_name == "column_500"
