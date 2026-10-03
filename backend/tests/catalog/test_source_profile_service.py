from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, delete, event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogKeyKind,
    CatalogSnapshot,
    CatalogSyncRun,
    SourceCatalogHead,
)
from app.db.models.source import DataSource, SourceStatus, SourceTableScope, TLSMode
from app.db.models.source_profile import (
    SourceProfileFinding,
    SourceProfileObservationStatus,
    SourceProfileRun,
    SourceProfileRunStatus,
    SourceProfileTableObservation,
)
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
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
    ProfileColumnAggregate,
    ProfileSelection,
    ProfileTableAggregate,
    SourceProfileInputError,
    SourceProfilingDisabledError,
    claim_next_source_profile_run,
    configure_source_profiling_for_owner,
    get_source_profile_run_for_owner,
    list_source_profile_findings_for_owner,
    list_source_profile_observations_for_owner,
    publish_source_profile_observations,
    renew_source_profile_lease,
    request_source_profile_run_for_owner,
)


_TABLES = [
    User.__table__,
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
    Base.metadata.tables["source_profile_runs"],
    Base.metadata.tables["source_profile_run_assets"],
    Base.metadata.tables["source_profile_run_columns"],
    Base.metadata.tables["source_profile_table_observations"],
    Base.metadata.tables["source_profile_column_observations"],
    Base.metadata.tables["source_profile_findings"],
]


@pytest.fixture
async def profile_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_TABLES)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="source-profile-service-test-key",
        database_url="sqlite+aiosqlite://",
        postgres_profile_sample_percent=1.0,
        postgres_profile_sample_limit=100,
        postgres_profile_max_tables_per_run=4,
        postgres_profile_max_columns_per_table=5,
        postgres_profile_max_columns_per_run=10,
        postgres_profile_max_operations_per_run=30,
    )


@pytest.mark.parametrize("sample_percent", (0.00001, 1.23456))
def test_profile_sample_setting_rejects_precision_that_persistence_would_round(sample_percent):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_key="source-profile-service-test-key",
            database_url="sqlite+aiosqlite://",
            postgres_profile_sample_percent=sample_percent,
        )
    assert (
        Settings(
            _env_file=None,
            api_key="source-profile-service-test-key",
            database_url="sqlite+aiosqlite://",
            postgres_profile_sample_percent=0.0001,
        ).postgres_profile_sample_percent
        == 0.0001
    )


def test_profile_provenance_constraints_bind_findings_to_their_exact_observations():
    """The database, not just service code, enforces cross-record provenance."""

    def foreign_key(table_name: str, constraint_name: str) -> ForeignKeyConstraint:
        return next(
            constraint
            for constraint in Base.metadata.tables[table_name].foreign_key_constraints
            if constraint.name == constraint_name
        )

    column_to_table = foreign_key(
        "source_profile_column_observations",
        "fk_source_profile_column_observations_table_provenance",
    )
    assert tuple(column_to_table.column_keys) == (
        "source_id",
        "run_id",
        "snapshot_id",
        "asset_id",
        "table_observation_id",
    )
    assert tuple(item.target_fullname for item in column_to_table.elements) == (
        "source_profile_table_observations.source_id",
        "source_profile_table_observations.run_id",
        "source_profile_table_observations.snapshot_id",
        "source_profile_table_observations.asset_id",
        "source_profile_table_observations.id",
    )

    finding_to_table = foreign_key(
        "source_profile_findings", "fk_source_profile_findings_table_provenance"
    )
    assert tuple(finding_to_table.column_keys) == (
        "source_id",
        "run_id",
        "table_observation_id",
    )
    finding_to_column = foreign_key(
        "source_profile_findings", "fk_source_profile_findings_column_provenance"
    )
    assert tuple(finding_to_column.column_keys) == (
        "source_id",
        "run_id",
        "table_observation_id",
        "column_observation_id",
    )


async def _owner(session, email: str = "owner@example.test") -> User:
    row = User(
        id=uuid.uuid4(),
        email=email,
        name=email.split("@", 1)[0],
        role=UserRole.USER,
        is_active=True,
        is_verified=True,
    )
    session.add(row)
    await session.commit()
    return row


async def _source(session, owner: User) -> DataSource:
    row = DataSource(
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
        profiling_generation=0,
        catalog_sync_sequence=0,
    )
    session.add(row)
    session.add_all(
        [
            SourceTableScope(source_id=row.id, schema_name="sales", table_name="orders"),
            SourceTableScope(source_id=row.id, schema_name="sales", table_name="customers"),
        ]
    )
    await session.commit()
    return row


def _column(name: str, ordinal: int, native_type: str) -> CatalogColumnInput:
    oid = {"int4": 23, "numeric": 1700, "text": 25, "jsonb": 3802}[native_type]
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


async def _publish_catalog(session, source: DataSource, owner: User) -> CatalogSnapshot:
    requested = await request_catalog_sync_for_owner(
        session, source_id=source.id, owner_user_id=owner.id
    )
    await mark_catalog_sync_running(session, source_id=source.id, run_id=requested.id)
    return await publish_catalog_snapshot(
        session,
        source_id=source.id,
        run_id=requested.id,
        snapshot_input=CatalogSnapshotInput(
            assets=(
                CatalogAssetInput(
                    identity=CatalogTableIdentity("sales", "customers"),
                    table_comment=None,
                    columns=(_column("customer_id", 1, "int4"),),
                    declared_keys=(
                        CatalogKeyInput(
                            constraint_name="customers_pkey",
                            key_kind=CatalogKeyKind.PRIMARY_KEY,
                            column_names=("customer_id",),
                        ),
                    ),
                ),
                CatalogAssetInput(
                    identity=CatalogTableIdentity("sales", "orders"),
                    table_comment=None,
                    columns=(
                        _column("order_id", 1, "int4"),
                        _column("amount", 2, "numeric"),
                        _column("status", 3, "text"),
                        _column("payload", 4, "jsonb"),
                    ),
                    declared_keys=(
                        CatalogKeyInput(
                            constraint_name="orders_pkey",
                            key_kind=CatalogKeyKind.PRIMARY_KEY,
                            column_names=("order_id",),
                        ),
                    ),
                ),
            )
        ),
    )


async def _asset_and_columns(session, snapshot: CatalogSnapshot, name: str = "orders"):
    asset = (
        await session.execute(
            select(CatalogAsset).where(
                CatalogAsset.source_id == snapshot.source_id,
                CatalogAsset.snapshot_id == snapshot.id,
                CatalogAsset.table_name == name,
            )
        )
    ).scalar_one()
    columns = (
        await session.execute(
            select(CatalogColumn)
            .where(CatalogColumn.asset_id == asset.id)
            .order_by(CatalogColumn.ordinal_position)
        )
    ).scalars().all()
    return asset, {column.column_name: column for column in columns}


async def _enabled_run(session, source: DataSource, owner: User):
    snapshot = await _publish_catalog(session, source, owner)
    source = await configure_source_profiling_for_owner(
        session,
        source_id=source.id,
        owner_user_id=owner.id,
        profiling_enabled=True,
    )
    asset, columns = await _asset_and_columns(session, snapshot)
    run = await request_source_profile_run_for_owner(
        session,
        source_id=source.id,
        owner_user_id=owner.id,
        selections=(
            ProfileSelection(
                asset_id=asset.id,
                column_ids=(columns["amount"].id, columns["status"].id),
            ),
        ),
        settings=_settings(),
    )
    return snapshot, asset, columns, source, run


async def test_profiles_are_default_off_and_selection_only_narrows_current_scope(profile_session):
    owner = await _owner(profile_session)
    source = await _source(profile_session, owner)
    snapshot = await _publish_catalog(profile_session, source, owner)
    asset, columns = await _asset_and_columns(profile_session, snapshot)
    source_id = source.id
    owner_id = owner.id
    asset_id = asset.id
    amount_id = columns["amount"].id
    status_id = columns["status"].id

    with pytest.raises(SourceProfilingDisabledError):
        await request_source_profile_run_for_owner(
            profile_session,
            source_id=source_id,
            owner_user_id=owner_id,
            selections=(ProfileSelection(asset_id, (amount_id,)),),
            settings=_settings(),
        )
    assert (
        await profile_session.execute(select(func.count()).select_from(SourceProfileRun))
    ).scalar_one() == 0

    configured = await configure_source_profiling_for_owner(
        profile_session,
        source_id=source_id,
        owner_user_id=owner_id,
        profiling_enabled=True,
    )
    assert configured.profiling_enabled is True
    assert configured.profiling_generation == 1
    run = await request_source_profile_run_for_owner(
        profile_session,
        source_id=source_id,
        owner_user_id=owner_id,
        selections=(ProfileSelection(asset_id, (amount_id, status_id)),),
        settings=_settings(),
    )
    assert run.status is SourceProfileRunStatus.QUEUED
    assert run.sample_limit == 100
    assert 0 < run.sample_percent <= 10
    assert 0 <= run.sample_seed <= 2_147_483_647
    assert run.selected_asset_count == 1
    assert run.selected_column_count == 2
    assert run.max_operations == 6  # one sample + numeric(3) + text(2)

    duplicate = await request_source_profile_run_for_owner(
        profile_session,
        source_id=source_id,
        owner_user_id=owner_id,
        selections=(ProfileSelection(asset_id, (status_id, amount_id)),),
        settings=_settings(),
    )
    assert duplicate.id == run.id
    with pytest.raises(SourceProfileInputError):
        await request_source_profile_run_for_owner(
            profile_session,
            source_id=source_id,
            owner_user_id=owner_id,
            selections=(ProfileSelection(uuid.uuid4(), ()),),
            settings=_settings(),
        )


async def test_aggregate_observations_are_sample_qualified_and_findings_are_traceable(profile_session):
    owner = await _owner(profile_session)
    source = await _source(profile_session, owner)
    _snapshot, asset, columns, _configured, run = await _enabled_run(profile_session, source, owner)
    claim = await claim_next_source_profile_run(
        profile_session, worker_id="profile-worker", lease_seconds=30, max_attempts=2
    )
    assert claim is not None and claim.run_id == run.id
    published = await publish_source_profile_observations(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        lease_token=claim.lease_token,
        table_aggregates=(
            ProfileTableAggregate(
                asset_id=asset.id,
                observed_row_count=10,
                status=SourceProfileObservationStatus.COMPLETE,
                columns=(
                    ProfileColumnAggregate(
                        column_id=columns["amount"].id,
                        status=SourceProfileObservationStatus.COMPLETE,
                        observed_non_null_count=8,
                        observed_null_count=2,
                        observed_distinct_count=4,
                        numeric_count=8,
                        numeric_sum=Decimal("120.5"),
                        numeric_minimum=Decimal("1.5"),
                        numeric_maximum=Decimal("50"),
                        numeric_mean=Decimal("15.0625"),
                    ),
                    ProfileColumnAggregate(
                        column_id=columns["status"].id,
                        status=SourceProfileObservationStatus.COMPLETE,
                        observed_non_null_count=10,
                        observed_null_count=0,
                        observed_distinct_count=1,
                    ),
                ),
            ),
        ),
    )
    assert published is True
    complete = await get_source_profile_run_for_owner(
        profile_session, source_id=source.id, run_id=run.id, owner_user_id=owner.id
    )
    assert complete.status is SourceProfileRunStatus.SUCCEEDED

    observations = await list_source_profile_observations_for_owner(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        owner_user_id=owner.id,
        page=1,
        page_size=10,
    )
    assert observations.total_tables == 1
    table = observations.table_observations[0].observation
    assert table.observed_row_count == 10
    assert table.population.value == "OBSERVED_SAMPLE"
    assert table.sample_seed == run.sample_seed
    assert "system_sampling_block_bias" in table.warning_codes
    assert {item.column.column_name for item in observations.column_observations} == {"amount", "status"}
    amount = next(item.observation for item in observations.column_observations if item.column.column_name == "amount")
    assert amount.null_count == 2
    assert amount.numeric_sum == Decimal("120.5000000000")

    findings = await list_source_profile_findings_for_owner(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        owner_user_id=owner.id,
        page=1,
        page_size=10,
    )
    assert findings.total == 2
    assert {item.finding.finding_type.value for item in findings.findings} == {
        "OBSERVED_NULL_RATE",
        "OBSERVED_CONSTANT_NON_NULL",
    }
    for item in findings.findings:
        assert item.finding.column_observation_id == item.column_observation.id
        assert item.finding.table_observation_id == item.table_observation.id
        assert "sample" in item.finding.limitations.lower()
        assert item.finding.evidence_json["population"] == "OBSERVED_SAMPLE"
        assert "whole-table" not in item.finding.description.lower()

    # Structural profile tables contain aggregates/provenance, never a raw
    # sampled value/example/top-values field.
    forbidden = {"raw_row", "sampled_row", "example_value", "top_values", "sql_text"}
    for db_table in (
        Base.metadata.tables["source_profile_runs"],
        Base.metadata.tables["source_profile_table_observations"],
        Base.metadata.tables["source_profile_column_observations"],
        Base.metadata.tables["source_profile_findings"],
    ):
        assert forbidden.isdisjoint(db_table.c.keys())


async def test_all_null_numeric_sample_keeps_aggregate_values_unavailable(profile_session):
    """All-null samples are truthful aggregate observations, never extrema."""

    owner = await _owner(profile_session)
    source = await _source(profile_session, owner)
    _snapshot, asset, columns, _configured, run = await _enabled_run(profile_session, source, owner)
    claim = await claim_next_source_profile_run(
        profile_session, worker_id="profile-worker", lease_seconds=30, max_attempts=2
    )
    assert claim is not None
    assert await publish_source_profile_observations(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        lease_token=claim.lease_token,
        table_aggregates=(
            ProfileTableAggregate(
                asset_id=asset.id,
                observed_row_count=3,
                status=SourceProfileObservationStatus.COMPLETE,
                columns=(
                    ProfileColumnAggregate(
                        column_id=columns["amount"].id,
                        status=SourceProfileObservationStatus.COMPLETE,
                        observed_non_null_count=0,
                        observed_null_count=3,
                        observed_distinct_count=0,
                        numeric_count=0,
                    ),
                    ProfileColumnAggregate(
                        column_id=columns["status"].id,
                        status=SourceProfileObservationStatus.COMPLETE,
                        observed_non_null_count=0,
                        observed_null_count=3,
                        observed_distinct_count=0,
                    ),
                ),
            ),
        ),
    )
    observations = await list_source_profile_observations_for_owner(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        owner_user_id=owner.id,
        page=1,
        page_size=10,
    )
    amount = next(
        item.observation
        for item in observations.column_observations
        if item.column.column_name == "amount"
    )
    assert amount.observed_row_count == 3
    assert amount.null_count == 3
    assert amount.non_null_count == amount.distinct_count == amount.numeric_count == 0
    assert amount.null_rate == Decimal("1.00000000")
    assert amount.numeric_sum is None
    assert amount.numeric_minimum is None
    assert amount.numeric_maximum is None
    assert amount.numeric_mean is None


async def test_profile_publication_rejects_a_population_above_the_saved_sample_limit(profile_session):
    owner = await _owner(profile_session)
    source = await _source(profile_session, owner)
    _snapshot, asset, columns, _configured, run = await _enabled_run(profile_session, source, owner)
    claim = await claim_next_source_profile_run(
        profile_session, worker_id="profile-worker", lease_seconds=30, max_attempts=2
    )
    assert claim is not None
    source_id = source.id
    run_id = run.id
    with pytest.raises(SourceProfileInputError, match="sample limit"):
        await publish_source_profile_observations(
            profile_session,
            source_id=source_id,
            run_id=run_id,
            lease_token=claim.lease_token,
            table_aggregates=(
                ProfileTableAggregate(
                    asset_id=asset.id,
                    observed_row_count=101,  # saved test limit is 100
                    status=SourceProfileObservationStatus.COMPLETE,
                    columns=(
                        ProfileColumnAggregate(
                            column_id=columns["amount"].id,
                            status=SourceProfileObservationStatus.COMPLETE,
                            observed_non_null_count=101,
                            observed_null_count=0,
                            observed_distinct_count=1,
                            numeric_count=101,
                            numeric_sum=Decimal("101"),
                            numeric_minimum=Decimal("1"),
                            numeric_maximum=Decimal("1"),
                            numeric_mean=Decimal("1"),
                        ),
                        ProfileColumnAggregate(
                            column_id=columns["status"].id,
                            status=SourceProfileObservationStatus.COMPLETE,
                            observed_non_null_count=101,
                            observed_null_count=0,
                            observed_distinct_count=1,
                        ),
                    ),
                ),
            ),
        )
    assert (
        await profile_session.execute(select(func.count()).select_from(SourceProfileTableObservation))
    ).scalar_one() == 0
    persisted = await profile_session.get(SourceProfileRun, run_id)
    assert persisted.status is SourceProfileRunStatus.RUNNING


async def test_cancellation_opt_out_and_scope_change_fence_publication(profile_session):
    owner = await _owner(profile_session)
    source = await _source(profile_session, owner)
    _snapshot, asset, columns, _configured, run = await _enabled_run(profile_session, source, owner)
    first = await claim_next_source_profile_run(
        profile_session, worker_id="worker-a", lease_seconds=30, max_attempts=2
    )
    assert first is not None
    source_id = source.id
    owner_id = owner.id
    run_id = run.id
    asset_id = asset.id
    amount_id = columns["amount"].id
    status_id = columns["status"].id

    # Opting out cancels the leased run and causes the next renewal/publish to
    # lose its fence; a stale remote result cannot land after the switch.
    await configure_source_profiling_for_owner(
        profile_session,
        source_id=source_id,
        owner_user_id=owner_id,
        profiling_enabled=False,
    )
    assert not await renew_source_profile_lease(
        profile_session,
        source_id=source_id,
        run_id=run_id,
        lease_token=first.lease_token,
        lease_seconds=30,
    )
    assert not await publish_source_profile_observations(
        profile_session,
        source_id=source_id,
        run_id=run_id,
        lease_token=first.lease_token,
        table_aggregates=(
            ProfileTableAggregate(
                asset_id=asset_id,
                observed_row_count=0,
                status=SourceProfileObservationStatus.EMPTY_SAMPLE,
                columns=(
                    ProfileColumnAggregate(
                        column_id=amount_id,
                        status=SourceProfileObservationStatus.EMPTY_SAMPLE,
                        observed_non_null_count=0,
                        observed_null_count=0,
                        observed_distinct_count=0,
                        numeric_count=0,
                    ),
                    ProfileColumnAggregate(
                        column_id=status_id,
                        status=SourceProfileObservationStatus.EMPTY_SAMPLE,
                        observed_non_null_count=0,
                        observed_null_count=0,
                        observed_distinct_count=0,
                    ),
                ),
            ),
        ),
    )
    cancelled = await get_source_profile_run_for_owner(
        profile_session, source_id=source_id, run_id=run_id, owner_user_id=owner_id
    )
    assert cancelled.status is SourceProfileRunStatus.CANCELLED
    assert (
        await profile_session.execute(select(func.count()).select_from(SourceProfileTableObservation))
    ).scalar_one() == 0


async def test_historical_observations_are_hidden_when_table_leaves_current_scope(profile_session):
    owner = await _owner(profile_session)
    source = await _source(profile_session, owner)
    _snapshot, asset, columns, _configured, run = await _enabled_run(profile_session, source, owner)
    claim = await claim_next_source_profile_run(
        profile_session, worker_id="profile-worker", lease_seconds=30, max_attempts=2
    )
    assert claim is not None
    assert await publish_source_profile_observations(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        lease_token=claim.lease_token,
        table_aggregates=(
            ProfileTableAggregate(
                asset_id=asset.id,
                observed_row_count=1,
                status=SourceProfileObservationStatus.COMPLETE,
                columns=(
                    ProfileColumnAggregate(
                        column_id=columns["amount"].id,
                        status=SourceProfileObservationStatus.COMPLETE,
                        observed_non_null_count=1,
                        observed_null_count=0,
                        observed_distinct_count=1,
                        numeric_count=1,
                        numeric_sum=Decimal("7"),
                        numeric_minimum=Decimal("7"),
                        numeric_maximum=Decimal("7"),
                        numeric_mean=Decimal("7"),
                    ),
                    ProfileColumnAggregate(
                        column_id=columns["status"].id,
                        status=SourceProfileObservationStatus.COMPLETE,
                        observed_non_null_count=1,
                        observed_null_count=0,
                        observed_distinct_count=1,
                    ),
                ),
            ),
        ),
    )
    await profile_session.execute(
        delete(SourceTableScope).where(
            SourceTableScope.source_id == source.id,
            SourceTableScope.schema_name == "sales",
            SourceTableScope.table_name == "orders",
        )
    )
    await profile_session.commit()
    observations = await list_source_profile_observations_for_owner(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        owner_user_id=owner.id,
        page=1,
        page_size=10,
    )
    findings = await list_source_profile_findings_for_owner(
        profile_session,
        source_id=source.id,
        run_id=run.id,
        owner_user_id=owner.id,
        page=1,
        page_size=10,
    )
    assert observations.total_tables == 0
    assert observations.table_observations == ()
    assert findings.total == 0


async def test_profile_reads_are_owner_scoped(profile_session):
    owner = await _owner(profile_session)
    other = await _owner(profile_session, "other@example.test")
    source = await _source(profile_session, owner)
    snapshot = await _publish_catalog(profile_session, source, owner)
    asset, columns = await _asset_and_columns(profile_session, snapshot)
    source_id = source.id
    owner_id = owner.id
    other_id = other.id
    asset_id = asset.id
    payload_id = columns["payload"].id
    amount_id = columns["amount"].id
    with pytest.raises(NotFoundError):
        await configure_source_profiling_for_owner(
            profile_session,
            source_id=source_id,
            owner_user_id=other_id,
            profiling_enabled=True,
        )
    await configure_source_profiling_for_owner(
        profile_session,
        source_id=source_id,
        owner_user_id=owner_id,
        profiling_enabled=True,
    )
    with pytest.raises(SourceProfileInputError):
        await request_source_profile_run_for_owner(
            profile_session,
            source_id=source_id,
            owner_user_id=owner_id,
            selections=(ProfileSelection(asset_id, (payload_id,)),),
            settings=_settings(),
        )
    run = await request_source_profile_run_for_owner(
        profile_session,
        source_id=source_id,
        owner_user_id=owner_id,
        selections=(ProfileSelection(asset_id, (amount_id,)),),
        settings=_settings(),
    )
    with pytest.raises(NotFoundError):
        await get_source_profile_run_for_owner(
            profile_session,
            source_id=source_id,
            run_id=run.id,
            owner_user_id=other_id,
        )
