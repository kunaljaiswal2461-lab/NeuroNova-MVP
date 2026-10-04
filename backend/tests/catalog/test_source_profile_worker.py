"""Durable worker integration tests for bounded aggregate source profiling."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.postgres_profiling_adapter import (
    ProfileColumnObservation,
    ProfileColumnStatus,
    ProfileExecutionResult,
    ProfileNumericSummary,
)
from app.core.config import Settings
from app.core.connector_credentials import CredentialBundle, CredentialProtector
from app.db.base import Base
from app.db.models.catalog import CatalogAsset, CatalogColumn, CatalogKeyKind, CatalogSnapshot, CatalogSyncRun
from app.db.models.source import DataSource, SourceCredential, SourceStatus, SourceTableScope, TLSMode
from app.db.models.source_profile import (
    SourceProfileColumnObservation,
    SourceProfileRun,
    SourceProfileRunStatus,
    SourceProfileTableObservation,
)
from app.db.models.user import User, UserRole
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogColumnInput,
    CatalogKeyInput,
    CatalogSnapshotInput,
    CatalogTableIdentity,
    mark_catalog_sync_running,
    publish_catalog_snapshot,
    request_catalog_sync_for_owner,
)
from app.services.source_profile_service import (
    ProfileSelection,
    cancel_source_profile_run_for_owner,
    claim_next_source_profile_run,
    configure_source_profiling_for_owner,
    request_source_profile_run_for_owner,
)
from app.workers.catalog_discovery_worker import CatalogDiscoveryWorker


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
    Base.metadata.tables["source_profile_runs"],
    Base.metadata.tables["source_profile_run_assets"],
    Base.metadata.tables["source_profile_run_columns"],
    Base.metadata.tables["source_profile_table_observations"],
    Base.metadata.tables["source_profile_column_observations"],
    Base.metadata.tables["source_profile_findings"],
]


@pytest.fixture
async def profile_worker_session_factory():
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
        api_key="stage6a-profile-worker-test-key",
        database_url="sqlite+aiosqlite://",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id="profile-test-key",
        connector_credentials_active_key=Fernet.generate_key().decode(),
        postgres_profile_sample_percent=1,
        postgres_profile_sample_limit=50,
        postgres_profile_max_tables_per_run=2,
        postgres_profile_max_columns_per_table=4,
        postgres_profile_max_columns_per_run=8,
        postgres_profile_max_operations_per_run=20,
        postgres_profile_operation_timeout_seconds=5,
        source_profile_worker_lease_seconds=5,
        source_profile_worker_lease_renewal_seconds=1,
        source_profile_worker_max_attempts=2,
        source_profile_worker_retry_base_seconds=1,
        source_profile_worker_retry_max_seconds=2,
    )


def _column(name: str, ordinal: int, native_type: str) -> CatalogColumnInput:
    oid = {"numeric": 1700, "text": 25}[native_type]
    return CatalogColumnInput(
        column_name=name,
        ordinal_position=ordinal,
        is_nullable=True,
        formatted_native_type=native_type,
        native_type_schema="pg_catalog",
        native_type_name=native_type,
        native_type_oid=oid,
        native_type_modifier=-1,
        column_comment=None,
    )


async def _setup_profile_run(factory, settings: Settings):
    async with factory() as session:
        owner = User(
            id=uuid.uuid4(),
            email="profile-worker@example.test",
            name="Profile worker owner",
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
            display_name="Profile source",
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
            table_scopes=[SourceTableScope(schema_name="sales", table_name="orders")],
        )
        # The source carries the owner UUID rather than an ORM relationship,
        # so persist the referenced user first for SQLite's immediate FK mode.
        session.add(owner)
        await session.commit()
        session.add(source)
        await session.commit()
        owner_id = owner.id

        catalog_run = await request_catalog_sync_for_owner(
            session, source_id=source_id, owner_user_id=owner_id
        )
        await mark_catalog_sync_running(session, source_id=source_id, run_id=catalog_run.id)
        snapshot = await publish_catalog_snapshot(
            session,
            source_id=source_id,
            run_id=catalog_run.id,
            snapshot_input=CatalogSnapshotInput(
                assets=(
                    CatalogAssetInput(
                        identity=CatalogTableIdentity("sales", "orders"),
                        table_comment=None,
                        columns=(
                            _column("amount", 1, "numeric"),
                            _column("status", 2, "text"),
                        ),
                        declared_keys=(
                            CatalogKeyInput(
                                constraint_name="orders_pkey",
                                key_kind=CatalogKeyKind.PRIMARY_KEY,
                                column_names=("amount",),
                            ),
                        ),
                    ),
                )
            ),
        )
        asset = (
            await session.execute(
                select(CatalogAsset).where(
                    CatalogAsset.source_id == source_id,
                    CatalogAsset.snapshot_id == snapshot.id,
                )
            )
        ).scalar_one()
        columns = {
            column.column_name: column
            for column in (
                await session.execute(
                    select(CatalogColumn).where(CatalogColumn.asset_id == asset.id)
                )
            ).scalars()
        }
        await configure_source_profiling_for_owner(
            session,
            source_id=source_id,
            owner_user_id=owner_id,
            profiling_enabled=True,
        )
        profile_run = await request_source_profile_run_for_owner(
            session,
            source_id=source_id,
            owner_user_id=owner_id,
            selections=(
                ProfileSelection(asset.id, (columns["amount"].id, columns["status"].id)),
            ),
            settings=settings,
        )
        return {
            "owner_id": owner_id,
            "source_id": source_id,
            "run_id": profile_run.id,
            "asset_id": asset.id,
            "amount_id": columns["amount"].id,
            "status_id": columns["status"].id,
        }


class _SuccessfulProfileAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, *, request, cancellation, **_kwargs):
        self.requests.append(request)
        assert cancellation is not None
        assert [operation.value for operation in request.operations] == [
            "ROW_COUNT",
            "COLUMN_NULL_DISTINCT",
            "NUMERIC_SUMMARY",
        ]
        by_name = {column.column_name: column for column in request.columns}
        return ProfileExecutionResult(
            collected_at=datetime.now(timezone.utc),
            sampling_method="table_sample_system",
            sample_percent=float(request.sampling.sample_percent),
            sample_limit=request.sampling.sample_limit,
            repeatable_seed=request.sampling.repeatable_seed,
            observed_row_count=5,
            measure_coverage="exact_for_observed_sample",
            columns=(
                ProfileColumnObservation(
                    column_id=by_name["amount"].column_id,
                    column_name="amount",
                    status=ProfileColumnStatus.SUPPORTED,
                    warning_codes=(),
                    observed_non_null_count=4,
                    observed_null_count=1,
                    observed_distinct_count=3,
                    numeric_summary=ProfileNumericSummary(
                        observed_non_null_count=4,
                        sum_value=Decimal("20"),
                        minimum_value=Decimal("1"),
                        maximum_value=Decimal("9"),
                        mean_value=Decimal("5"),
                    ),
                ),
                ProfileColumnObservation(
                    column_id=by_name["status"].column_id,
                    column_name="status",
                    status=ProfileColumnStatus.SUPPORTED,
                    warning_codes=(),
                    observed_non_null_count=5,
                    observed_null_count=0,
                    observed_distinct_count=2,
                    numeric_summary=None,
                ),
            ),
        )


async def test_shared_worker_publishes_only_fenced_aggregate_profile_results(
    profile_worker_session_factory,
):
    settings = _settings()
    context = await _setup_profile_run(profile_worker_session_factory, settings)
    adapter = _SuccessfulProfileAdapter()
    worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=profile_worker_session_factory,
        adapter_factory=lambda _settings: None,
        profiling_adapter_factory=lambda _settings: adapter,
        worker_id="profile-worker-success",
    )

    assert await worker.run_once()
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.relation.schema_name == "sales"
    assert request.relation.table_name == "orders"
    assert request.sampling.sample_limit == 50

    async with profile_worker_session_factory() as session:
        run = await session.get(SourceProfileRun, context["run_id"])
        assert run.status is SourceProfileRunStatus.SUCCEEDED
        assert run.lease_token is None
        assert (
            await session.execute(select(func.count()).select_from(SourceProfileTableObservation))
        ).scalar_one() == 1
        amount = (
            await session.execute(
                select(SourceProfileColumnObservation).where(
                    SourceProfileColumnObservation.column_id == context["amount_id"]
                )
            )
        ).scalar_one()
        assert amount.numeric_sum == Decimal("20.0000000000")
        assert amount.null_count == 1
        forbidden = {"raw_row", "sampled_row", "example_value", "top_values", "sql_text"}
        assert forbidden.isdisjoint(SourceProfileColumnObservation.__table__.c.keys())


async def test_worker_cancellation_terminates_remote_profile_and_prevents_publication(
    profile_worker_session_factory,
):
    settings = _settings()
    context = await _setup_profile_run(profile_worker_session_factory, settings)
    started = asyncio.Event()
    stopped = asyncio.Event()

    class _BlockingProfileAdapter:
        async def execute(self, *, cancellation, **_kwargs):
            started.set()
            try:
                while not cancellation.cancelled:
                    await asyncio.sleep(0.01)
            finally:
                stopped.set()
            raise AssertionError("cancelled profiling should not return an aggregate result")

    worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=profile_worker_session_factory,
        adapter_factory=lambda _settings: None,
        profiling_adapter_factory=lambda _settings: _BlockingProfileAdapter(),
        worker_id="profile-worker-cancel",
    )
    async with profile_worker_session_factory() as session:
        claim = await claim_next_source_profile_run(
            session,
            worker_id="profile-worker-cancel",
            lease_seconds=5,
            max_attempts=2,
        )
    assert claim is not None

    task = asyncio.create_task(worker._process_profile_claim(claim))
    await asyncio.wait_for(started.wait(), timeout=1)
    async with profile_worker_session_factory() as session:
        await cancel_source_profile_run_for_owner(
            session,
            source_id=context["source_id"],
            run_id=context["run_id"],
            owner_user_id=context["owner_id"],
        )
    await asyncio.wait_for(task, timeout=3)
    assert stopped.is_set()

    async with profile_worker_session_factory() as session:
        run = await session.get(SourceProfileRun, context["run_id"])
        assert run.status is SourceProfileRunStatus.CANCELLED
        assert (
            await session.execute(select(func.count()).select_from(SourceProfileTableObservation))
        ).scalar_one() == 0
