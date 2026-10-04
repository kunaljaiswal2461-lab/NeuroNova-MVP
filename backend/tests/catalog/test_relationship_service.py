"""Focused Stage 5B owner-confirmed relationship boundary tests.

SQLite exercises durable application/catalog state only.  These tests do not
claim that a mocked catalog proves PostgreSQL's live FK or uniqueness checks;
the execution slice owns those disposable-PostgreSQL checks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    RelationshipDefinitionCreateRequest,
    RelationshipDefinitionVersionCreateRequest,
)
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
from app.services.relationship_service import (
    RelationshipNeedsReviewError,
    RelationshipRetiredError,
    RelationshipValidationError,
    create_relationship_definition_for_owner,
    create_relationship_definition_version_for_owner,
    get_relationship_definition_for_owner,
    list_eligible_declared_relationships_for_owner,
    list_relationship_definition_versions_for_owner,
    list_relationship_definitions_for_owner,
    list_resolved_relationships_for_base_asset,
    resolve_relationship_definition_for_owner,
    retire_relationship_definition_for_owner,
    verify_relationship_pin_for_execution,
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
    RelationshipDefinition.__table__,
    RelationshipDefinitionVersion.__table__,
]


@dataclass(frozen=True)
class _RelationshipCatalog:
    session: AsyncSession
    owner_id: uuid.UUID
    other_owner_id: uuid.UUID
    source_id: uuid.UUID
    orders: CatalogTableIdentity
    customers: CatalogTableIdentity
    snapshot: CatalogSnapshot
    order_asset: CatalogAsset
    customer_asset: CatalogAsset
    foreign_key: CatalogForeignKey


@pytest.fixture
async def relationship_session() -> AsyncSession:
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
    identities: tuple[CatalogTableIdentity, ...],
) -> DataSource:
    source = DataSource(
        id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        source_type=SourceType.POSTGRESQL,
        display_name="Relationship service synthetic source",
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
        SourceTableScope(
            source_id=source.id,
            schema_name=identity.schema_name,
            table_name=identity.table_name,
        )
        for identity in identities
    )
    await session.commit()
    return source


def _column(
    name: str,
    ordinal: int,
    *,
    native_type_name: str = "int4",
    native_type_oid: int = 23,
    formatted_native_type: str = "integer",
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
    *,
    changed_target_key_name: bool = False,
    lookup_in_scope: bool = True,
    target_has_supported_key: bool = True,
) -> CatalogSnapshotInput:
    customers = CatalogTableIdentity("sales", "customers")
    orders = CatalogTableIdentity("sales", "orders")
    customer_asset = CatalogAssetInput(
        identity=customers,
        table_comment=None,
        columns=(
            _column("tenant_id", 1),
            _column("customer_id", 2),
            _column(
                "region",
                3,
                native_type_name="text",
                native_type_oid=25,
                formatted_native_type="text",
            ),
        ),
        declared_keys=(
            (
                CatalogKeyInput(
                    constraint_name=("customers_key_changed" if changed_target_key_name else "customers_pkey"),
                    key_kind=CatalogKeyKind.PRIMARY_KEY,
                    column_names=("tenant_id", "customer_id"),
                ),
            )
            if target_has_supported_key
            else ()
        ),
    )
    orders_asset = CatalogAssetInput(
        identity=orders,
        table_comment=None,
        columns=(
            _column("order_id", 1),
            _column("tenant_id", 2),
            _column("customer_id", 3, nullable=True),
            _column(
                "amount",
                4,
                native_type_name="numeric",
                native_type_oid=1700,
                formatted_native_type="numeric(12,2)",
            ),
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
                target_status=(
                    CatalogForeignKeyTargetStatus.IN_SCOPE
                    if lookup_in_scope
                    else CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE
                ),
                referenced_table=customers if lookup_in_scope else None,
                column_mappings=(
                    (
                        CatalogForeignKeyColumnInput(1, "tenant_id", "tenant_id"),
                        CatalogForeignKeyColumnInput(2, "customer_id", "customer_id"),
                    )
                    if lookup_in_scope
                    else ()
                ),
            ),
        ),
    )
    return CatalogSnapshotInput(
        assets=(customer_asset, orders_asset) if lookup_in_scope else (orders_asset,)
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


async def _asset_and_fk(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> tuple[CatalogAsset, CatalogAsset, CatalogForeignKey]:
    assets = (
        await session.execute(
            select(CatalogAsset).where(
                CatalogAsset.source_id == source_id,
                CatalogAsset.snapshot_id == snapshot_id,
            )
        )
    ).scalars().all()
    by_name = {asset.table_name: asset for asset in assets}
    fk = (
        await session.execute(
            select(CatalogForeignKey).where(
                CatalogForeignKey.source_id == source_id,
                CatalogForeignKey.snapshot_id == snapshot_id,
                CatalogForeignKey.constraint_name == "orders_customer_fkey",
            )
        )
    ).scalar_one()
    return by_name["orders"], by_name["customers"], fk


@pytest.fixture
async def relationship_catalog(relationship_session: AsyncSession) -> _RelationshipCatalog:
    owner = await _user(relationship_session, "relationship-owner@example.test")
    other = await _user(relationship_session, "relationship-other@example.test")
    orders = CatalogTableIdentity("sales", "orders")
    customers = CatalogTableIdentity("sales", "customers")
    source = await _source(
        relationship_session,
        owner_user_id=owner.id,
        identities=(orders, customers),
    )
    snapshot = await _publish(
        relationship_session,
        source_id=source.id,
        owner_user_id=owner.id,
        snapshot_input=_snapshot_input(),
    )
    order_asset, customer_asset, foreign_key = await _asset_and_fk(
        relationship_session,
        source_id=source.id,
        snapshot_id=snapshot.id,
    )
    return _RelationshipCatalog(
        session=relationship_session,
        owner_id=owner.id,
        other_owner_id=other.id,
        source_id=source.id,
        orders=orders,
        customers=customers,
        snapshot=snapshot,
        order_asset=order_asset,
        customer_asset=customer_asset,
        foreign_key=foreign_key,
    )


def _request(
    catalog: _RelationshipCatalog,
    *,
    owner_confirmed: bool = True,
    join_type: str = "left",
    snapshot_id: uuid.UUID | None = None,
    foreign_key_id: uuid.UUID | None = None,
) -> RelationshipDefinitionCreateRequest:
    return RelationshipDefinitionCreateRequest.model_validate(
        {
            "owner_confirmed": owner_confirmed,
            "snapshot_id": snapshot_id or catalog.snapshot.id,
            "foreign_key_id": foreign_key_id or catalog.foreign_key.id,
            "join_type": join_type,
        }
    )


async def test_confirmed_declared_composite_fk_is_versioned_owner_scoped_and_retired(
    relationship_catalog: _RelationshipCatalog,
):
    catalog = relationship_catalog
    with pytest.raises(ValidationError):
        _request(catalog, owner_confirmed=False)

    eligible = await list_eligible_declared_relationships_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        owner_user_id=catalog.owner_id,
    )
    assert len(eligible) == 1
    assert eligible[0].foreign_key_id == catalog.foreign_key.id
    assert eligible[0].target_key.key_kind == "primary_key"
    assert [pair.base_column_name for pair in eligible[0].column_pairs] == [
        "tenant_id",
        "customer_id",
    ]

    created = await create_relationship_definition_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        owner_user_id=catalog.owner_id,
        request=_request(catalog),
    )
    assert created.version_number == 1
    assert created.state == "valid"
    assert created.join_type == "left"
    assert created.target_key_kind == "primary_key"

    resolved = await resolve_relationship_definition_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
        snapshot_id=catalog.snapshot.id,
    )
    assert resolved.base_asset_id == catalog.order_asset.id
    assert resolved.lookup_asset_id == catalog.customer_asset.id
    assert [(pair.base_column_name, pair.lookup_column_name) for pair in resolved.column_pairs] == [
        ("tenant_id", "tenant_id"),
        ("customer_id", "customer_id"),
    ]
    role_columns = (
        await catalog.session.execute(
            select(CatalogColumn).where(
                CatalogColumn.source_id == catalog.source_id,
                CatalogColumn.snapshot_id == catalog.snapshot.id,
            )
        )
    ).scalars().all()
    by_asset_and_name = {(column.asset_id, column.column_name): column.id for column in role_columns}
    # Full role-scoped surfaces permit an approved lookup attribute such as
    # customers.region while the two FK pairs remain the only ON mapping.
    assert by_asset_and_name[(catalog.order_asset.id, "amount")] in resolved.allowed_base_column_ids
    assert (
        by_asset_and_name[(catalog.customer_asset.id, "region")]
        in resolved.allowed_lookup_column_ids
    )
    assert await verify_relationship_pin_for_execution(
        catalog.session,
        owner_user_id=catalog.owner_id,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        version_number=created.version_number,
        relationship_version_id=resolved.relationship_version_id,
        snapshot_id=catalog.snapshot.id,
    )

    versioned = await create_relationship_definition_version_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
        request=RelationshipDefinitionVersionCreateRequest.model_validate(
            _request(catalog, join_type="inner").model_dump()
        ),
    )
    assert versioned.version_number == 2
    assert versioned.join_type == "inner"
    history = await list_relationship_definition_versions_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
    )
    assert [item.version_number for item in history] == [2, 1]
    with pytest.raises(RelationshipNeedsReviewError):
        await resolve_relationship_definition_for_owner(
            catalog.session,
            source_id=catalog.source_id,
            relationship_id=created.relationship_id,
            owner_user_id=catalog.owner_id,
            version_number=1,
            require_current=True,
        )

    active_from_orders = await list_resolved_relationships_for_base_asset(
        catalog.session,
        source_id=catalog.source_id,
        owner_user_id=catalog.owner_id,
        base_asset_id=catalog.order_asset.id,
        snapshot_id=catalog.snapshot.id,
    )
    assert [item.relationship_id for item in active_from_orders] == [created.relationship_id]

    with pytest.raises(NotFoundError):
        await get_relationship_definition_for_owner(
            catalog.session,
            source_id=catalog.source_id,
            relationship_id=created.relationship_id,
            owner_user_id=catalog.other_owner_id,
        )
    with pytest.raises(NotFoundError):
        await list_relationship_definitions_for_owner(
            catalog.session,
            source_id=catalog.source_id,
            owner_user_id=catalog.other_owner_id,
        )

    retired = await retire_relationship_definition_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
    )
    assert retired.state == "retired"
    with pytest.raises(RelationshipRetiredError):
        await resolve_relationship_definition_for_owner(
            catalog.session,
            source_id=catalog.source_id,
            relationship_id=created.relationship_id,
            owner_user_id=catalog.owner_id,
        )


async def test_relationship_revalidates_snapshot_rotation_changed_key_and_scope_removal(
    relationship_catalog: _RelationshipCatalog,
):
    catalog = relationship_catalog
    created = await create_relationship_definition_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        owner_user_id=catalog.owner_id,
        request=_request(catalog),
    )

    compatible_snapshot = await _publish(
        catalog.session,
        source_id=catalog.source_id,
        owner_user_id=catalog.owner_id,
        snapshot_input=_snapshot_input(),
    )
    compatible_orders, _, _ = await _asset_and_fk(
        catalog.session,
        source_id=catalog.source_id,
        snapshot_id=compatible_snapshot.id,
    )
    compatible = await get_relationship_definition_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
    )
    assert compatible.state == "valid"
    assert compatible.current_catalog is not None
    assert compatible.current_catalog.snapshot_id == compatible_snapshot.id
    assert compatible.current_catalog.base_asset_id == compatible_orders.id

    changed_key_snapshot = await _publish(
        catalog.session,
        source_id=catalog.source_id,
        owner_user_id=catalog.owner_id,
        snapshot_input=_snapshot_input(changed_target_key_name=True),
    )
    assert changed_key_snapshot.id != compatible_snapshot.id
    needs_review = await get_relationship_definition_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
    )
    assert needs_review.state == "needs_review"
    assert needs_review.needs_review_reason == "target_key_missing"
    with pytest.raises(RelationshipNeedsReviewError):
        await resolve_relationship_definition_for_owner(
            catalog.session,
            source_id=catalog.source_id,
            relationship_id=created.relationship_id,
            owner_user_id=catalog.owner_id,
        )

    # A fresh confirmation is required after a current key contract changes.
    changed_orders, _changed_customers, changed_fk = await _asset_and_fk(
        catalog.session,
        source_id=catalog.source_id,
        snapshot_id=changed_key_snapshot.id,
    )
    reconfirmed = await create_relationship_definition_version_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
        request=RelationshipDefinitionVersionCreateRequest.model_validate(
            {
                "owner_confirmed": True,
                "snapshot_id": changed_key_snapshot.id,
                "foreign_key_id": changed_fk.id,
                "join_type": "left",
            }
        ),
    )
    assert reconfirmed.version_number == 2
    assert reconfirmed.state == "valid"
    assert changed_orders.id != catalog.order_asset.id

    # Removing the lookup from current scope and publishing that new scope
    # makes the relationship unusable; it cannot silently retain its old ID.
    await catalog.session.execute(
        delete(SourceTableScope).where(
            SourceTableScope.source_id == catalog.source_id,
            SourceTableScope.schema_name == catalog.customers.schema_name,
            SourceTableScope.table_name == catalog.customers.table_name,
        )
    )
    await catalog.session.commit()
    await _publish(
        catalog.session,
        source_id=catalog.source_id,
        owner_user_id=catalog.owner_id,
        snapshot_input=_snapshot_input(lookup_in_scope=False),
    )
    scope_removed = await get_relationship_definition_for_owner(
        catalog.session,
        source_id=catalog.source_id,
        relationship_id=created.relationship_id,
        owner_user_id=catalog.owner_id,
    )
    assert scope_removed.state == "needs_review"
    assert scope_removed.needs_review_reason == "asset_unavailable"


async def test_relationship_rejects_undeclared_nonunique_and_cross_source_references(
    relationship_catalog: _RelationshipCatalog,
):
    catalog = relationship_catalog
    with pytest.raises(RelationshipValidationError):
        await create_relationship_definition_for_owner(
            catalog.session,
            source_id=catalog.source_id,
            owner_user_id=catalog.owner_id,
            request=_request(catalog, foreign_key_id=uuid.uuid4()),
        )

    # A declared FK without matching declared target PK/UNIQUE evidence is
    # never eligible merely because an owner can see or label it.
    no_key_source = await _source(
        catalog.session,
        owner_user_id=catalog.owner_id,
        identities=(catalog.orders, catalog.customers),
    )
    no_key_snapshot = await _publish(
        catalog.session,
        source_id=no_key_source.id,
        owner_user_id=catalog.owner_id,
        snapshot_input=_snapshot_input(target_has_supported_key=False),
    )
    _no_key_orders, _no_key_customers, no_key_fk = await _asset_and_fk(
        catalog.session,
        source_id=no_key_source.id,
        snapshot_id=no_key_snapshot.id,
    )
    assert await list_eligible_declared_relationships_for_owner(
        catalog.session,
        source_id=no_key_source.id,
        owner_user_id=catalog.owner_id,
    ) == ()
    with pytest.raises(RelationshipValidationError):
        await create_relationship_definition_for_owner(
            catalog.session,
            source_id=no_key_source.id,
            owner_user_id=catalog.owner_id,
            request=RelationshipDefinitionCreateRequest.model_validate(
                {
                    "owner_confirmed": True,
                    "snapshot_id": no_key_snapshot.id,
                    "foreign_key_id": no_key_fk.id,
                    "join_type": "left",
                }
            ),
        )

    # A different owner/source cannot use a valid snapshot-scoped FK UUID.
    other_source = await _source(
        catalog.session,
        owner_user_id=catalog.other_owner_id,
        identities=(catalog.orders, catalog.customers),
    )
    other_snapshot = await _publish(
        catalog.session,
        source_id=other_source.id,
        owner_user_id=catalog.other_owner_id,
        snapshot_input=_snapshot_input(),
    )
    with pytest.raises(RelationshipValidationError):
        await create_relationship_definition_for_owner(
            catalog.session,
            source_id=other_source.id,
            owner_user_id=catalog.other_owner_id,
            request=RelationshipDefinitionCreateRequest.model_validate(
                {
                    "owner_confirmed": True,
                    "snapshot_id": other_snapshot.id,
                    "foreign_key_id": catalog.foreign_key.id,
                    "join_type": "left",
                }
            ),
        )
