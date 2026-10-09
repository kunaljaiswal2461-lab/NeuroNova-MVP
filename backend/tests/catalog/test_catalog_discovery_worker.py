from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.postgres_adapter import (
    DiscoveredColumn,
    DiscoveredForeignKey,
    DiscoveredForeignKeyMapping,
    DiscoveredKey,
    DiscoveredTable,
    SelectedTableUnavailableError,
)
from app.core.config import Settings
from app.core.connector_credentials import CredentialBundle, CredentialProtector
from app.db.base import Base
from app.db.models.catalog import CatalogAsset, CatalogSnapshot, CatalogSyncRun, CatalogSyncRunStatus, SourceCatalogHead
from app.db.models.source import DataSource, SourceCredential, SourceStatus, SourceTableScope, TLSMode
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.services.catalog_service import (
    CatalogRunStateError,
    CatalogRunLeaseLostError,
    CatalogSnapshotInput,
    cancel_catalog_sync_run_for_owner,
    claim_next_catalog_sync_run,
    finalize_catalog_sync_worker_failure,
    get_current_catalog_snapshot_for_owner,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    renew_catalog_sync_lease,
    request_catalog_sync_for_owner,
)
from app.workers.catalog_discovery_worker import CatalogDiscoveryWorker, _to_catalog_snapshot


_TABLES = [
    User.__table__,
    DataSource.__table__,
    SourceCredential.__table__,
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
    Base.metadata.tables["catalog_column_samples"],
    Base.metadata.tables["source_sample_exclusions"],
]


@pytest.fixture
async def worker_session_factory():
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


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="catalog-worker-test-key",
        database_url="sqlite+aiosqlite://",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id="test-key",
        connector_credentials_active_key=Fernet.generate_key().decode(),
        catalog_worker_lease_seconds=5,
        catalog_worker_lease_renewal_seconds=1,
        catalog_worker_max_attempts=3,
        catalog_worker_retry_base_seconds=1,
        catalog_worker_retry_max_seconds=2,
    )


async def _make_source(factory, settings: Settings):
    async with factory() as session:
        owner = User(
            id=uuid.uuid4(),
            email="owner@example.test",
            name="Owner",
            role=UserRole.USER,
            is_active=True,
            is_verified=True,
        )
        source_id = uuid.uuid4()
        ciphertext, key_id, encryption_format = CredentialProtector.from_settings(settings).encrypt(
            CredentialBundle(
                source_id=source_id,
                username="reader",
                password="synthetic-password",
                custom_ca_pem="SYNTHETIC CA",
            )
        )
        source = DataSource(
            id=source_id,
            owner_user_id=owner.id,
            display_name="Warehouse",
            host="warehouse.example.test",
            port=5432,
            database_name="analytics",
            tls_mode=TLSMode.VERIFY_FULL,
            has_custom_ca=True,
            status=SourceStatus.TESTED,
            profiling_enabled=False,
            credential=SourceCredential(
                ciphertext=ciphertext,
                key_id=key_id,
                encryption_format=encryption_format,
            ),
            table_scopes=[
                SourceTableScope(schema_name="sales", table_name="customers"),
                SourceTableScope(schema_name="sales", table_name="orders"),
            ],
        )
        session.add(owner)
        await session.commit()
        session.add(source)
        await session.commit()
        return owner.id, source_id


def _metadata() -> tuple[DiscoveredTable, ...]:
    return (
        DiscoveredTable(
            schema_name="sales",
            table_name="customers",
            table_comment="Customer source comment",
            columns=(
                DiscoveredColumn("tenant_id", 1, False, "integer", "pg_catalog", "int4", 23, -1, None),
                DiscoveredColumn("customer_id", 2, False, "integer", "pg_catalog", "int4", 23, -1, "Customer id"),
            ),
            declared_keys=(DiscoveredKey("customers_pkey", "p", ("tenant_id", "customer_id")),),
            foreign_keys=(),
        ),
        DiscoveredTable(
            schema_name="sales",
            table_name="orders",
            table_comment="Order source comment",
            columns=(
                DiscoveredColumn("order_id", 1, False, "integer", "pg_catalog", "int4", 23, -1, None),
                DiscoveredColumn("tenant_id", 2, False, "integer", "pg_catalog", "int4", 23, -1, None),
                DiscoveredColumn("customer_id", 3, False, "integer", "pg_catalog", "int4", 23, -1, None),
            ),
            declared_keys=(DiscoveredKey("orders_pkey", "p", ("order_id",)),),
            foreign_keys=(
                DiscoveredForeignKey(
                    constraint_name="orders_customer_fkey",
                    target_in_scope=True,
                    referenced_schema_name="sales",
                    referenced_table_name="customers",
                    column_mappings=(
                        DiscoveredForeignKeyMapping(1, "tenant_id", "tenant_id"),
                        DiscoveredForeignKeyMapping(2, "customer_id", "customer_id"),
                    ),
                ),
                DiscoveredForeignKey(
                    constraint_name="orders_excluded_target_fkey",
                    target_in_scope=False,
                    referenced_schema_name=None,
                    referenced_table_name=None,
                    column_mappings=(),
                ),
            ),
        ),
    )


class _Adapter:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def discover_selected_tables(self, *, selected_tables, **_kwargs):
        self.calls += 1
        assert {(table.schema_name, table.table_name) for table in selected_tables} == {
            ("sales", "customers"),
            ("sales", "orders"),
        }
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _request_run(factory, source_id: uuid.UUID, owner_id: uuid.UUID):
    async with factory() as session:
        return await request_catalog_sync_for_owner(
            session, source_id=source_id, owner_user_id=owner_id
        )


async def test_durable_claim_recovery_retry_and_stale_fencing(worker_session_factory):
    settings = _settings()
    owner_id, source_id = await _make_source(worker_session_factory, settings)
    first_request = await _request_run(worker_session_factory, source_id, owner_id)
    duplicate_request = await _request_run(worker_session_factory, source_id, owner_id)
    assert duplicate_request.id == first_request.id

    async with worker_session_factory() as session:
        first = await claim_next_catalog_sync_run(
            session, worker_id="worker-a", lease_seconds=5, max_attempts=3
        )
    assert first is not None and first.attempt_count == 1
    async with worker_session_factory() as session:
        assert await renew_catalog_sync_lease(
            session,
            source_id=source_id,
            run_id=first.run_id,
            lease_token=first.lease_token,
            lease_seconds=5,
        )
        run = await session.get(CatalogSyncRun, first.run_id)
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    async with worker_session_factory() as session:
        recovered = await claim_next_catalog_sync_run(
            session, worker_id="worker-b", lease_seconds=5, max_attempts=3
        )
    assert recovered is not None
    assert recovered.attempt_count == 2
    assert recovered.lease_token != first.lease_token
    async with worker_session_factory() as session:
        with pytest.raises(CatalogRunLeaseLostError):
            await publish_catalog_snapshot(
                session,
                source_id=source_id,
                run_id=first.run_id,
                snapshot_input=_to_catalog_snapshot(_metadata()),
                lease_token=first.lease_token,
            )
        assert (await session.execute(select(func.count()).select_from(CatalogSnapshot))).scalar_one() == 0
    async with worker_session_factory() as session:
        assert not await finalize_catalog_sync_worker_failure(
            session,
            source_id=source_id,
            run_id=first.run_id,
            lease_token=first.lease_token,
            failure_code="connection_failed",
            retry_delay_seconds=0,
            max_attempts=3,
        )
        assert await finalize_catalog_sync_worker_failure(
            session,
            source_id=source_id,
            run_id=recovered.run_id,
            lease_token=recovered.lease_token,
            failure_code="connection_failed",
            retry_delay_seconds=0,
            max_attempts=3,
        )
    async with worker_session_factory() as session:
        final_claim = await claim_next_catalog_sync_run(
            session, worker_id="worker-c", lease_seconds=5, max_attempts=3
        )
        assert final_claim is not None and final_claim.attempt_count == 3
        assert await finalize_catalog_sync_worker_failure(
            session,
            source_id=source_id,
            run_id=final_claim.run_id,
            lease_token=final_claim.lease_token,
            failure_code="connection_failed",
            retry_delay_seconds=0,
            max_attempts=3,
        )
        run = await session.get(CatalogSyncRun, final_claim.run_id)
        assert run.status is CatalogSyncRunStatus.FAILED
        assert run.failure_code == "connection_failed"


async def test_worker_recovers_a_pre_lease_running_run(worker_session_factory):
    settings = _settings()
    owner_id, source_id = await _make_source(worker_session_factory, settings)
    run = await _request_run(worker_session_factory, source_id, owner_id)
    async with worker_session_factory() as session:
        await mark_catalog_sync_running(session, source_id=source_id, run_id=run.id)
    async with worker_session_factory() as session:
        recovered = await claim_next_catalog_sync_run(
            session, worker_id="worker-legacy-recovery", lease_seconds=5, max_attempts=3
        )

    assert recovered is not None
    assert recovered.run_id == run.id
    assert recovered.attempt_count == 1


async def test_worker_discovers_normalizes_and_publishes_complete_catalog(worker_session_factory):
    settings = _settings()
    owner_id, source_id = await _make_source(worker_session_factory, settings)
    run = await _request_run(worker_session_factory, source_id, owner_id)
    adapter = _Adapter(_metadata())
    worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=worker_session_factory,
        adapter_factory=lambda _settings: adapter,
        worker_id="worker-success",
    )

    assert await worker.run_once()
    assert adapter.calls == 1
    async with worker_session_factory() as session:
        persisted = await session.get(CatalogSyncRun, run.id)
        assert persisted.status is CatalogSyncRunStatus.SUCCEEDED
        head = await get_current_catalog_snapshot_for_owner(
            session, source_id=source_id, owner_user_id=owner_id
        )
        assert head is not None and head.sync_run_id == run.id
        assets = list(
            (
                await session.execute(
                    select(CatalogAsset).where(CatalogAsset.snapshot_id == head.id)
                )
            ).scalars()
        )
        assert {(asset.schema_name, asset.table_name) for asset in assets} == {
            ("sales", "customers"),
            ("sales", "orders"),
        }


async def test_worker_failure_and_scope_change_preserve_previous_head(worker_session_factory):
    settings = _settings()
    owner_id, source_id = await _make_source(worker_session_factory, settings)
    first_run = await _request_run(worker_session_factory, source_id, owner_id)
    first_worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=worker_session_factory,
        adapter_factory=lambda _settings: _Adapter(_metadata()),
        worker_id="worker-first",
    )
    assert await first_worker.run_once()
    async with worker_session_factory() as session:
        first_head = await get_current_catalog_snapshot_for_owner(
            session, source_id=source_id, owner_user_id=owner_id
        )
        first_snapshot_id = first_head.id

    failed_run = await _request_run(worker_session_factory, source_id, owner_id)
    failed_worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=worker_session_factory,
        adapter_factory=lambda _settings: _Adapter(SelectedTableUnavailableError()),
        worker_id="worker-failure",
    )
    assert await failed_worker.run_once()
    async with worker_session_factory() as session:
        persisted_failed = await session.get(CatalogSyncRun, failed_run.id)
        assert persisted_failed.status is CatalogSyncRunStatus.FAILED
        assert persisted_failed.failure_code == "selected_table_unavailable"
        head = await get_current_catalog_snapshot_for_owner(
            session, source_id=source_id, owner_user_id=owner_id
        )
        assert head.id == first_snapshot_id

    changed_run = await _request_run(worker_session_factory, source_id, owner_id)

    class _ScopeChangingAdapter(_Adapter):
        async def discover_selected_tables(self, **kwargs):
            result = await super().discover_selected_tables(**kwargs)
            async with worker_session_factory() as session:
                scope = (
                    await session.execute(
                        select(SourceTableScope).where(
                            SourceTableScope.source_id == source_id,
                            SourceTableScope.table_name == "orders",
                        )
                    )
                ).scalar_one()
                await session.delete(scope)
                await session.commit()
            return result

    changed_worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=worker_session_factory,
        adapter_factory=lambda _settings: _ScopeChangingAdapter(_metadata()),
        worker_id="worker-scope-change",
    )
    assert await changed_worker.run_once()
    async with worker_session_factory() as session:
        persisted_changed = await session.get(CatalogSyncRun, changed_run.id)
        assert persisted_changed.status is CatalogSyncRunStatus.FAILED
        assert persisted_changed.failure_code == "scope_changed"
        head = await get_current_catalog_snapshot_for_owner(
            session, source_id=source_id, owner_user_id=owner_id
        )
        assert head.id == first_snapshot_id


async def test_cancellation_during_discovery_cancels_remote_task_and_prevents_publication(worker_session_factory):
    settings = _settings()
    owner_id, source_id = await _make_source(worker_session_factory, settings)
    run = await _request_run(worker_session_factory, source_id, owner_id)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _BlockingAdapter:
        async def discover_selected_tables(self, **_kwargs):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=worker_session_factory,
        adapter_factory=lambda _settings: _BlockingAdapter(),
        worker_id="worker-cancel",
    )
    async with worker_session_factory() as session:
        claim = await claim_next_catalog_sync_run(
            session, worker_id="worker-cancel", lease_seconds=5, max_attempts=3
        )
    task = asyncio.create_task(worker._process_claim(claim))
    await asyncio.wait_for(started.wait(), timeout=1)
    async with worker_session_factory() as session:
        await cancel_catalog_sync_run_for_owner(
            session,
            source_id=source_id,
            run_id=run.id,
            owner_user_id=owner_id,
        )
    await asyncio.wait_for(task, timeout=3)
    assert cancelled.is_set()
    async with worker_session_factory() as session:
        persisted = await session.get(CatalogSyncRun, run.id)
        assert persisted.status is CatalogSyncRunStatus.CANCELLED
        assert (await session.execute(select(func.count()).select_from(CatalogSnapshot))).scalar_one() == 0
        with pytest.raises(CatalogRunStateError):
            await publish_catalog_snapshot(
                session,
                source_id=source_id,
                run_id=run.id,
                snapshot_input=_to_catalog_snapshot(_metadata()),
                lease_token=claim.lease_token,
            )


async def test_cancellation_before_discovery_prevents_connector_call(worker_session_factory):
    settings = _settings()
    owner_id, source_id = await _make_source(worker_session_factory, settings)
    run = await _request_run(worker_session_factory, source_id, owner_id)
    async with worker_session_factory() as session:
        claim = await claim_next_catalog_sync_run(
            session, worker_id="worker-pre-cancel", lease_seconds=5, max_attempts=3
        )
    async with worker_session_factory() as session:
        await cancel_catalog_sync_run_for_owner(
            session,
            source_id=source_id,
            run_id=run.id,
            owner_user_id=owner_id,
        )
    adapter = _Adapter(_metadata())
    worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=worker_session_factory,
        adapter_factory=lambda _settings: adapter,
        worker_id="worker-pre-cancel",
    )

    await worker._process_claim(claim)

    assert adapter.calls == 0
    async with worker_session_factory() as session:
        persisted = await session.get(CatalogSyncRun, run.id)
        assert persisted.status is CatalogSyncRunStatus.CANCELLED
        assert (await session.execute(select(func.count()).select_from(CatalogSnapshot))).scalar_one() == 0


async def test_worker_source_deletion_cascades_claim_before_remote_discovery(worker_session_factory):
    settings = _settings()
    owner_id, source_id = await _make_source(worker_session_factory, settings)
    run = await _request_run(worker_session_factory, source_id, owner_id)
    async with worker_session_factory() as session:
        claim = await claim_next_catalog_sync_run(
            session, worker_id="worker-delete", lease_seconds=5, max_attempts=3
        )
        source = await session.get(DataSource, source_id)
        await session.delete(source)
        await session.commit()
    worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=worker_session_factory,
        adapter_factory=lambda _settings: _Adapter(_metadata()),
        worker_id="worker-delete",
    )
    await worker._process_claim(claim)
    async with worker_session_factory() as session:
        assert await session.get(CatalogSyncRun, run.id) is None
        with pytest.raises(NotFoundError):
            await get_current_catalog_snapshot_for_owner(
                session, source_id=source_id, owner_user_id=owner_id
            )
