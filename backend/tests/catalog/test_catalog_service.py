from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogDeclaredKeyColumn,
    CatalogDeclaredKey,
    CatalogForeignKey,
    CatalogForeignKeyColumn,
    CatalogForeignKeyTargetStatus,
    CatalogKeyKind,
    CatalogSnapshot,
    CatalogSyncRun,
    CatalogSyncRunStatus,
    SourceCatalogHead,
)
from app.db.models.conversation import ConversationSessionRow
from app.db.models.dataset import DatasetRecord
from app.db.models.source import DataSource, SourceCredential, SourceStatus, SourceTableScope, TLSMode
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.services.account_service import delete_account
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogColumnInput,
    CatalogForeignKeyColumnInput,
    CatalogForeignKeyInput,
    CatalogInputError,
    CatalogKeyInput,
    CatalogPersistenceError,
    CatalogRunStateError,
    CatalogRunSupersededError,
    CatalogScopeChangedError,
    CatalogSnapshotInput,
    CatalogTableIdentity,
    cancel_catalog_sync_run_for_owner,
    canonical_scope_fingerprint,
    get_catalog_snapshot_for_owner,
    get_current_catalog_snapshot_for_owner,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    request_catalog_sync_for_owner,
)
from agentic_engine.tools.uploader.storage import LocalStorage


_CATALOG_TABLES = [
    User.__table__,
    DatasetRecord.__table__,
    ConversationSessionRow.__table__,
    DataSource.__table__,
    SourceCredential.__table__,
    SourceTableScope.__table__,
    # Importing the models above registers all catalog tables in metadata;
    # name lookup keeps this fixture explicit and avoids unrelated tables.
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
async def catalog_session():
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
        display_name="Synthetic warehouse",
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


def _column(name: str, ordinal: int, *, nullable: bool = False) -> CatalogColumnInput:
    return CatalogColumnInput(
        column_name=name,
        ordinal_position=ordinal,
        is_nullable=nullable,
        formatted_native_type="integer",
        native_type_schema="pg_catalog",
        native_type_name="int4",
        native_type_oid=23,
        native_type_modifier=-1,
        column_comment=f"{name} comment",
    )


def _catalog_input(
    *,
    customer_table_comment: str = "Descriptive customer source comment.",
) -> CatalogSnapshotInput:
    customers = CatalogTableIdentity("sales", "customers")
    orders = CatalogTableIdentity("sales", "orders")
    return CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=customers,
                table_comment=customer_table_comment,
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
                table_comment="Descriptive order source comment.",
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
                    CatalogKeyInput(
                        constraint_name="orders_customer_unique",
                        key_kind=CatalogKeyKind.UNIQUE,
                        column_names=("tenant_id", "customer_id"),
                    ),
                ),
                foreign_keys=(
                    CatalogForeignKeyInput(
                        constraint_name="orders_customer_fkey",
                        target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                        referenced_table=customers,
                        column_mappings=(
                            CatalogForeignKeyColumnInput(1, "tenant_id", "tenant_id"),
                            CatalogForeignKeyColumnInput(2, "customer_id", "customer_id"),
                        ),
                    ),
                    CatalogForeignKeyInput(
                        constraint_name="orders_target_outside_scope",
                        target_status=CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE,
                    ),
                ),
            ),
        )
    )


async def _publish(session, source: DataSource, owner: User, snapshot_input: CatalogSnapshotInput | None = None):
    run = await request_catalog_sync_for_owner(
        session, source_id=source.id, owner_user_id=owner.id
    )
    await mark_catalog_sync_running(session, source_id=source.id, run_id=run.id)
    snapshot = await publish_catalog_snapshot(
        session,
        source_id=source.id,
        run_id=run.id,
        snapshot_input=snapshot_input or _catalog_input(),
    )
    return run, snapshot


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_persists_complete_catalog_with_ordered_composite_keys_and_relationships(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    source = await _make_source(catalog_session, owner=owner)

    run, snapshot = await _publish(catalog_session, source, owner)

    assert run.status is CatalogSyncRunStatus.SUCCEEDED
    assert snapshot.source_id == source.id
    assert snapshot.sync_run_id == run.id
    assert snapshot.scope_fingerprint == run.scope_fingerprint
    assert (
        await get_current_catalog_snapshot_for_owner(
            catalog_session, source_id=source.id, owner_user_id=owner.id
        )
    ).id == snapshot.id

    assets = list(
        (
            await catalog_session.execute(
                select(CatalogAsset)
                .where(CatalogAsset.snapshot_id == snapshot.id)
                .order_by(CatalogAsset.table_name)
            )
        ).scalars()
    )
    assert [(asset.schema_name, asset.table_name) for asset in assets] == [
        ("sales", "customers"),
        ("sales", "orders"),
    ]
    assert assets[0].table_comment == "Descriptive customer source comment."

    key_columns = list(
        (
            await catalog_session.execute(
                select(CatalogDeclaredKeyColumn)
                .where(CatalogDeclaredKeyColumn.snapshot_id == snapshot.id)
                .order_by(CatalogDeclaredKeyColumn.key_id, CatalogDeclaredKeyColumn.ordinal_position)
            )
        ).scalars()
    )
    assert any(
        [row.ordinal_position for row in key_columns[index:index + 2]] == [1, 2]
        for index in range(len(key_columns) - 1)
    )

    mappings = list(
        (
            await catalog_session.execute(
                select(CatalogForeignKeyColumn)
                .where(CatalogForeignKeyColumn.snapshot_id == snapshot.id)
                .order_by(CatalogForeignKeyColumn.ordinal_position)
            )
        ).scalars()
    )
    assert [mapping.ordinal_position for mapping in mappings] == [1, 2]
    omitted = (
        await catalog_session.execute(
            select(CatalogForeignKey).where(
                CatalogForeignKey.snapshot_id == snapshot.id,
                CatalogForeignKey.target_status == CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE,
            )
        )
    ).scalar_one()
    assert omitted.referenced_asset_id is None
    assert (
        await catalog_session.execute(
            select(func.count())
            .select_from(CatalogForeignKeyColumn)
            .where(CatalogForeignKeyColumn.foreign_key_id == omitted.id)
        )
    ).scalar_one() == 0


def test_scope_fingerprint_uses_structured_identifier_pairs():
    first = canonical_scope_fingerprint(
        [CatalogTableIdentity("a.b", "c"), CatalogTableIdentity("a", "b.c")]
    )
    reordered = canonical_scope_fingerprint(
        [CatalogTableIdentity("a", "b.c"), CatalogTableIdentity("a.b", "c")]
    )
    left = canonical_scope_fingerprint([CatalogTableIdentity("a.b", "c")])
    right = canonical_scope_fingerprint([CatalogTableIdentity("a", "b.c")])

    assert first == reordered
    assert left != right


async def test_rejects_scope_exclusion_and_preserves_unusual_identifier_parts(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    weird = CatalogTableIdentity('a.b', 'orders:"2026"')
    source = await _make_source(catalog_session, owner=owner, scope=(weird,))
    source_id = source.id
    valid = CatalogSnapshotInput(
        assets=(
            CatalogAssetInput(
                identity=weird,
                columns=(_column('id"part', 1),),
                declared_keys=(
                    CatalogKeyInput(
                        constraint_name='orders:"2026"_pkey',
                        key_kind=CatalogKeyKind.PRIMARY_KEY,
                        column_names=('id"part',),
                    ),
                ),
            ),
        )
    )
    run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner.id
    )
    run_id = run.id
    await mark_catalog_sync_running(catalog_session, source_id=source_id, run_id=run_id)

    with pytest.raises(CatalogInputError, match="exactly match"):
        await publish_catalog_snapshot(
            catalog_session,
            source_id=source_id,
            run_id=run_id,
            snapshot_input=_catalog_input(),
        )
    assert await _count(catalog_session, CatalogSnapshot) == 0

    # The failed run remains retryable; a complete matching snapshot preserves
    # each component exactly instead of creating a dotted identity string.
    snapshot = await publish_catalog_snapshot(
        catalog_session, source_id=source_id, run_id=run_id, snapshot_input=valid
    )
    asset = (
        await catalog_session.execute(select(CatalogAsset).where(CatalogAsset.snapshot_id == snapshot.id))
    ).scalar_one()
    assert (asset.schema_name, asset.table_name) == ('a.b', 'orders:"2026"')


async def test_database_constraints_reject_cross_source_and_cross_snapshot_references(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    first_source = await _make_source(catalog_session, owner=owner)
    second_source = await _make_source(catalog_session, owner=owner)
    _first_run, first_snapshot = await _publish(catalog_session, first_source, owner)
    _second_run, second_snapshot = await _publish(catalog_session, second_source, owner)
    first_source_id = first_source.id
    first_snapshot_id = first_snapshot.id
    second_snapshot_id = second_snapshot.id
    first_asset = (
        await catalog_session.execute(
            select(CatalogAsset).where(
                CatalogAsset.snapshot_id == first_snapshot_id,
                CatalogAsset.table_name == "orders",
            )
        )
    ).scalar_one()
    second_asset = (
        await catalog_session.execute(
            select(CatalogAsset).where(
                CatalogAsset.snapshot_id == second_snapshot_id,
                CatalogAsset.table_name == "orders",
            )
        )
    ).scalar_one()
    first_asset_id = first_asset.id
    second_asset_id = second_asset.id

    catalog_session.add(
        CatalogAsset(
            id=uuid.uuid4(),
            source_id=first_source_id,
            snapshot_id=second_snapshot_id,
            schema_name="bad",
            table_name="cross_snapshot",
        )
    )
    with pytest.raises(IntegrityError):
        await catalog_session.flush()
    await catalog_session.rollback()

    first_head = await catalog_session.get(SourceCatalogHead, first_source_id)
    first_head.snapshot_id = second_snapshot_id
    with pytest.raises(IntegrityError):
        await catalog_session.flush()
    await catalog_session.rollback()

    catalog_session.add(
        CatalogForeignKey(
            id=uuid.uuid4(),
            source_id=first_source_id,
            snapshot_id=first_snapshot_id,
            referencing_asset_id=first_asset_id,
            constraint_name="bad_cross_source_target",
            target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
            referenced_asset_id=second_asset_id,
        )
    )
    with pytest.raises(IntegrityError):
        await catalog_session.flush()
    await catalog_session.rollback()


async def test_failed_publish_rolls_back_new_snapshot_and_preserves_prior_head(catalog_session, monkeypatch):
    owner = await _make_user(catalog_session, "owner@example.test")
    source = await _make_source(catalog_session, owner=owner)
    _first_run, first_snapshot = await _publish(catalog_session, source, owner)
    source_id = source.id
    owner_id = owner.id
    first_snapshot_id = first_snapshot.id
    run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    run_id = run.id
    await mark_catalog_sync_running(catalog_session, source_id=source_id, run_id=run_id)
    original_flush = catalog_session.flush

    async def fail_snapshot_flush(*_args, **_kwargs):
        raise IntegrityError("INSERT catalog_snapshots", {}, RuntimeError("synthetic persistence failure"))

    monkeypatch.setattr(catalog_session, "flush", fail_snapshot_flush)
    with pytest.raises(CatalogPersistenceError):
        await publish_catalog_snapshot(
            catalog_session,
            source_id=source_id,
            run_id=run_id,
            snapshot_input=_catalog_input(),
        )
    monkeypatch.setattr(catalog_session, "flush", original_flush)

    head = await get_current_catalog_snapshot_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    assert head is not None and head.id == first_snapshot_id
    assert await _count(catalog_session, CatalogSnapshot) == 1


async def test_superseded_cancelled_and_duplicate_runs_cannot_replace_current_snapshot(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    source = await _make_source(catalog_session, owner=owner)
    source_id = source.id
    owner_id = owner.id
    old_run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    old_run_id = old_run.id
    await mark_catalog_sync_running(catalog_session, source_id=source_id, run_id=old_run_id)
    newer_run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    newer_run_id = newer_run.id

    with pytest.raises(CatalogRunSupersededError):
        await publish_catalog_snapshot(
            catalog_session,
            source_id=source_id,
            run_id=old_run_id,
            snapshot_input=_catalog_input(),
        )
    assert await _count(catalog_session, CatalogSnapshot) == 0

    await mark_catalog_sync_running(catalog_session, source_id=source_id, run_id=newer_run_id)
    snapshot = await publish_catalog_snapshot(
        catalog_session,
        source_id=source_id,
        run_id=newer_run_id,
        snapshot_input=_catalog_input(),
    )
    with pytest.raises(CatalogRunStateError):
        await publish_catalog_snapshot(
            catalog_session,
            source_id=source_id,
            run_id=newer_run_id,
            snapshot_input=_catalog_input(),
        )

    cancelled_run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    cancelled_run_id = cancelled_run.id
    await mark_catalog_sync_running(catalog_session, source_id=source_id, run_id=cancelled_run_id)
    await cancel_catalog_sync_run_for_owner(
        catalog_session,
        source_id=source_id,
        run_id=cancelled_run_id,
        owner_user_id=owner_id,
    )
    with pytest.raises(CatalogRunStateError):
        await publish_catalog_snapshot(
            catalog_session,
            source_id=source_id,
            run_id=cancelled_run_id,
            snapshot_input=_catalog_input(),
        )
    head = await get_current_catalog_snapshot_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    assert head is not None and head.id == snapshot.id
    assert await _count(catalog_session, SourceCatalogHead) == 1


async def test_scope_change_after_start_fails_run_and_preserves_previous_snapshot(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    source = await _make_source(catalog_session, owner=owner)
    _first_run, first_snapshot = await _publish(catalog_session, source, owner)
    source_id = source.id
    owner_id = owner.id
    first_snapshot_id = first_snapshot.id

    run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    run_id = run.id
    await mark_catalog_sync_running(catalog_session, source_id=source_id, run_id=run_id)
    await catalog_session.execute(
        delete(SourceTableScope).where(
            SourceTableScope.source_id == source_id,
            SourceTableScope.table_name == "orders",
        )
    )
    await catalog_session.commit()

    with pytest.raises(CatalogScopeChangedError):
        await publish_catalog_snapshot(
            catalog_session,
            source_id=source_id,
            run_id=run_id,
            snapshot_input=_catalog_input(),
        )

    persisted_run = await catalog_session.get(CatalogSyncRun, run_id)
    assert persisted_run.status is CatalogSyncRunStatus.FAILED
    assert persisted_run.failure_code == "scope_changed"
    head = await get_current_catalog_snapshot_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    assert head is not None and head.id == first_snapshot_id
    assert await _count(catalog_session, CatalogSnapshot) == 1


async def test_null_owner_cannot_request_or_cancel_a_catalog_run(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    other = await _make_user(catalog_session, "other@example.test")
    source = await _make_source(catalog_session, owner=owner)
    source_id = source.id
    owner_id = owner.id
    other_id = other.id

    with pytest.raises(NotFoundError):
        await request_catalog_sync_for_owner(
            catalog_session,
            source_id=source_id,
            owner_user_id=other_id,
        )
    with pytest.raises(NotFoundError):
        await request_catalog_sync_for_owner(
            catalog_session,
            source_id=source_id,
            owner_user_id=None,  # type: ignore[arg-type]
        )
    assert await _count(catalog_session, CatalogSyncRun) == 0

    run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner_id
    )
    run_id = run.id
    with pytest.raises(NotFoundError):
        await cancel_catalog_sync_run_for_owner(
            catalog_session,
            source_id=source_id,
            run_id=run_id,
            owner_user_id=other_id,
        )
    with pytest.raises(NotFoundError):
        await cancel_catalog_sync_run_for_owner(
            catalog_session,
            source_id=source_id,
            run_id=run_id,
            owner_user_id=None,  # type: ignore[arg-type]
        )
    persisted_run = await catalog_session.get(CatalogSyncRun, run_id)
    assert persisted_run.status is CatalogSyncRunStatus.QUEUED


async def test_successive_publications_preserve_historical_snapshot_and_owner_scope(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    other = await _make_user(catalog_session, "other@example.test")
    source = await _make_source(catalog_session, owner=owner)
    source_id = source.id
    first_input = _catalog_input(customer_table_comment="First structural observation")
    _first_run, first_snapshot = await _publish(catalog_session, source, owner, first_input)
    second_run = await request_catalog_sync_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner.id
    )
    await mark_catalog_sync_running(catalog_session, source_id=source_id, run_id=second_run.id)
    second_snapshot = await publish_catalog_snapshot(
        catalog_session,
        source_id=source_id,
        run_id=second_run.id,
        snapshot_input=_catalog_input(customer_table_comment="Second structural observation"),
    )

    assert second_snapshot.id != first_snapshot.id
    assert await _count(catalog_session, CatalogSnapshot) == 2
    current = await get_current_catalog_snapshot_for_owner(
        catalog_session, source_id=source_id, owner_user_id=owner.id
    )
    assert current is not None and current.id == second_snapshot.id
    historical = await get_catalog_snapshot_for_owner(
        catalog_session,
        source_id=source_id,
        snapshot_id=first_snapshot.id,
        owner_user_id=owner.id,
    )
    first_asset = (
        await catalog_session.execute(
            select(CatalogAsset).where(
                CatalogAsset.snapshot_id == historical.id,
                CatalogAsset.table_name == "customers",
            )
        )
    ).scalar_one()
    assert first_asset.table_comment == "First structural observation"
    with pytest.raises(NotFoundError):
        await get_catalog_snapshot_for_owner(
            catalog_session,
            source_id=source_id,
            snapshot_id=first_snapshot.id,
            owner_user_id=other.id,
        )


async def test_owner_scoped_reads_and_source_delete_cascade_catalog_records(catalog_session):
    owner = await _make_user(catalog_session, "owner@example.test")
    other = await _make_user(catalog_session, "other@example.test")
    source = await _make_source(catalog_session, owner=owner)
    _run, snapshot = await _publish(catalog_session, source, owner)
    source_id = source.id
    owner_id = owner.id
    other_id = other.id
    snapshot_id = snapshot.id

    with pytest.raises(NotFoundError):
        await get_current_catalog_snapshot_for_owner(
            catalog_session, source_id=source_id, owner_user_id=other_id
        )
    with pytest.raises(NotFoundError):
        await get_current_catalog_snapshot_for_owner(
            catalog_session, source_id=source_id, owner_user_id=None  # type: ignore[arg-type]
        )
    source_to_delete = await catalog_session.get(DataSource, source_id)
    await catalog_session.delete(source_to_delete)
    await catalog_session.commit()
    for model in (
        SourceCatalogHead,
        CatalogSyncRun,
        CatalogSnapshot,
        CatalogAsset,
        CatalogColumn,
        CatalogDeclaredKey,
        CatalogDeclaredKeyColumn,
        CatalogForeignKey,
        CatalogForeignKeyColumn,
    ):
        assert await _count(catalog_session, model) == 0
    assert snapshot_id


async def test_account_deletion_bulk_source_cleanup_cascades_catalog_records(catalog_session, tmp_path: Path):
    owner = await _make_user(catalog_session, "owner@example.test")
    source = await _make_source(catalog_session, owner=owner)
    await _publish(catalog_session, source, owner)
    settings = Settings(
        _env_file=None,
        api_key="catalog-account-delete-test-key",
        database_url="sqlite+aiosqlite://",
        data_dir=tmp_path,
    )
    settings.ensure_data_dirs()

    await delete_account(
        session=catalog_session,
        storage=LocalStorage(settings.raw_dir),
        settings=settings,
        user=owner,
    )

    for model in (
        SourceCatalogHead,
        CatalogSyncRun,
        CatalogSnapshot,
        CatalogAsset,
        CatalogColumn,
        CatalogDeclaredKey,
        CatalogDeclaredKeyColumn,
        CatalogForeignKey,
        CatalogForeignKeyColumn,
    ):
        assert await _count(catalog_session, model) == 0
    assert await _count(catalog_session, DataSource) == 0
