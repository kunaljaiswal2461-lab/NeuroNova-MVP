"""Shared durable worker for PostgreSQL catalog discovery and profiling.

Run this module in a separately supervised process with ``python -m
app.workers.catalog_discovery_worker``. It claims database-backed catalog and
explicitly requested profile runs; the FastAPI web process only
creates/cancels/reads those rows. Profile work shares this process's durable
credential and lease mechanics rather than introducing a second framework.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.connectors.connection_policy import ConnectionPolicyError
from app.connectors.postgres_adapter import (
    DiscoveredTable,
    MetadataAccessError,
    MetadataDiscoveryTimeoutError,
    MetadataLimitError,
    PostgresAdapterError,
    PostgresCredentialFailure,
    PostgresOnboardingAdapter,
    PostgresTLSFailure,
    PostgresTimeoutError,
    SelectedTableUnavailableError,
    InventoryTable,
)
from app.connectors.postgres_profiling_adapter import (
    ProfileColumnStatus,
    ProfileExecutionBudget,
    ProfileOperation,
    ProfileRequest,
    ProfileSamplingConfig,
    ProfiledColumn,
    ProfiledRelation,
    PostgresProfilingAdapter,
    PostgresProfilingRequestError,
)
from app.connectors.postgres_query_executor import (
    PostgresExecutionAccessChangedError,
    PostgresExecutionAdapterError,
    PostgresExecutionCancellation,
    PostgresExecutionCancelledError,
    PostgresExecutionConfigurationError,
    PostgresExecutionCredentialError,
    PostgresExecutionRestrictedRoleError,
    PostgresExecutionSchemaChangedError,
    PostgresExecutionTLSFailureError,
    PostgresExecutionTimeoutError,
)
from app.core.config import Settings, get_settings
from app.core.connector_credentials import (
    CredentialConfigurationError,
    CredentialProtector,
    CredentialUnavailableError,
    InvalidCredentialBundleError,
)
from app.core.logging import configure_logging, get_logger
from app.db.models.catalog import CatalogForeignKeyTargetStatus, CatalogKeyKind
from app.db.models.source import DataSource
from app.db.models.source_profile import SourceProfileObservationStatus
from app.db.session import get_sessionmaker
from app.exceptions.custom_exceptions import NotFoundError
from app.services.catalog_service import (
    CatalogAssetInput,
    CatalogColumnInput,
    CatalogForeignKeyColumnInput,
    CatalogForeignKeyInput,
    CatalogInputError,
    CatalogKeyInput,
    CatalogPersistenceError,
    CatalogRunLeaseLostError,
    CatalogRunStateError,
    CatalogRunSupersededError,
    CatalogScopeChangedError,
    CatalogSnapshotInput,
    CatalogSyncLease,
    CatalogTableIdentity,
    claim_next_catalog_sync_run,
    finalize_catalog_sync_worker_failure,
    publish_catalog_snapshot,
    renew_catalog_sync_lease,
)
from app.services.source_profile_service import (
    ProfileColumnAggregate,
    ProfileExecutionContext,
    ProfileTableAggregate,
    SourceProfileInputError,
    SourceProfileLease,
    SourceProfileLeaseLostError,
    SourceProfilePersistenceError,
    SourceProfileSupersededError,
    claim_next_source_profile_run,
    finalize_source_profile_worker_failure,
    load_source_profile_execution_context,
    publish_source_profile_observations,
    renew_source_profile_lease,
)


logger = get_logger(__name__)


class CatalogWorkerConfigurationError(RuntimeError):
    """The separately deployed worker cannot safely perform connector I/O."""


class CatalogLeaseLost(RuntimeError):
    """A cancellation, expiry, or replacement fenced this worker instance."""


@dataclass(frozen=True)
class DiscoveryContext:
    source_id: uuid.UUID
    host: str
    port: int
    database_name: str
    username: str
    password: str
    custom_ca_pem: str | None
    selected_tables: tuple[InventoryTable, ...]


@dataclass(frozen=True)
class ProfileConnectionContext:
    """Decrypted one-attempt connection settings; never persisted or logged."""

    source_id: uuid.UUID
    host: str
    port: int
    database_name: str
    username: str
    password: str
    custom_ca_pem: str | None


AdapterFactory = Callable[[Settings], PostgresOnboardingAdapter]
ProfilingAdapterFactory = Callable[[Settings], PostgresProfilingAdapter]


def _build_adapter(settings: Settings) -> PostgresOnboardingAdapter:
    return PostgresOnboardingAdapter(settings=settings)


def _build_profiling_adapter(settings: Settings) -> PostgresProfilingAdapter:
    return PostgresProfilingAdapter(settings=settings)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"[:128]


def _require_worker_credentials(settings: Settings) -> CredentialProtector:
    if not settings.connectors_enabled or not settings.connector_egress_policy_configured:
        raise CatalogWorkerConfigurationError("connector operations are not configured")
    try:
        return CredentialProtector.from_settings(settings)
    except CredentialConfigurationError as exc:
        raise CatalogWorkerConfigurationError("credential protection is not configured") from exc


def _to_catalog_snapshot(discovered_tables: tuple[DiscoveredTable, ...]) -> CatalogSnapshotInput:
    """Convert bounded connector metadata to the Stage 3A typed boundary."""

    assets: list[CatalogAssetInput] = []
    for table in discovered_tables:
        identity = CatalogTableIdentity(table.schema_name, table.table_name)
        columns = tuple(
            CatalogColumnInput(
                column_name=column.column_name,
                ordinal_position=column.ordinal_position,
                is_nullable=column.is_nullable,
                formatted_native_type=column.formatted_native_type,
                native_type_schema=column.native_type_schema,
                native_type_name=column.native_type_name,
                native_type_oid=column.native_type_oid,
                native_type_modifier=column.native_type_modifier,
                column_comment=column.column_comment,
            )
            for column in table.columns
        )
        keys = tuple(
            CatalogKeyInput(
                constraint_name=key.constraint_name,
                key_kind=(
                    CatalogKeyKind.PRIMARY_KEY if key.key_kind == "p" else CatalogKeyKind.UNIQUE
                ),
                column_names=key.column_names,
            )
            for key in table.declared_keys
        )
        foreign_keys: list[CatalogForeignKeyInput] = []
        for foreign_key in table.foreign_keys:
            if not foreign_key.target_in_scope:
                foreign_keys.append(
                    CatalogForeignKeyInput(
                        constraint_name=foreign_key.constraint_name,
                        target_status=CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE,
                    )
                )
                continue
            if foreign_key.referenced_schema_name is None or foreign_key.referenced_table_name is None:
                raise CatalogInputError("in-scope foreign key target is missing")
            foreign_keys.append(
                CatalogForeignKeyInput(
                    constraint_name=foreign_key.constraint_name,
                    target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                    referenced_table=CatalogTableIdentity(
                        foreign_key.referenced_schema_name,
                        foreign_key.referenced_table_name,
                    ),
                    column_mappings=tuple(
                        CatalogForeignKeyColumnInput(
                            ordinal_position=mapping.ordinal_position,
                            referencing_column_name=mapping.referencing_column_name,
                            referenced_column_name=mapping.referenced_column_name,
                        )
                        for mapping in foreign_key.column_mappings
                    ),
                )
            )
        assets.append(
            CatalogAssetInput(
                identity=identity,
                table_comment=table.table_comment,
                columns=columns,
                declared_keys=keys,
                foreign_keys=tuple(foreign_keys),
            )
        )
    return CatalogSnapshotInput(assets=tuple(assets))


class CatalogDiscoveryWorker:
    """One explicitly started process consuming catalog and profile requests."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        adapter_factory: AdapterFactory | None = None,
        profiling_adapter_factory: ProfilingAdapterFactory | None = None,
        worker_id: str | None = None,
    ):
        self._settings = settings
        self._session_factory = session_factory
        self._adapter_factory = adapter_factory or _build_adapter
        self._profiling_adapter_factory = profiling_adapter_factory or _build_profiling_adapter
        self._worker_id = worker_id or _worker_id()
        self._protector = _require_worker_credentials(settings)

    async def run_once(self) -> bool:
        """Claim one catalog run first, otherwise one profile run."""

        async with self._session_factory() as session:
            claim = await claim_next_catalog_sync_run(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._settings.catalog_worker_lease_seconds,
                max_attempts=self._settings.catalog_worker_max_attempts,
            )
        if claim is not None:
            await self._process_claim(claim)
            return True
        async with self._session_factory() as session:
            profile_claim = await claim_next_source_profile_run(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._settings.source_profile_worker_lease_seconds,
                max_attempts=self._settings.source_profile_worker_max_attempts,
            )
        if profile_claim is None:
            return False
        await self._process_profile_claim(profile_claim)
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Maintain bounded process-local task concurrency over durable jobs."""

        tasks: set[asyncio.Task[None]] = set()
        catalog_tasks: set[asyncio.Task[None]] = set()
        profile_tasks: set[asyncio.Task[None]] = set()
        # One shared process-level cap preserves the Stage 3 durable-worker
        # deployment model. Per-kind caps additionally bound profile reads.
        profile_limit = min(
            self._settings.source_profile_worker_max_concurrent_jobs,
            self._settings.postgres_profile_max_concurrent_operations,
        )
        total_limit = max(
            self._settings.catalog_worker_max_concurrent_jobs,
            profile_limit,
        )
        # This is one shared durable loop. Respect the more responsive of the
        # two bounded queue-poll settings rather than leaving the profile
        # interval as an ineffective configuration field.
        poll_interval = min(
            self._settings.catalog_worker_poll_interval_seconds,
            self._settings.source_profile_worker_poll_interval_seconds,
        )
        prefer_profile = False

        def _finish(task: asyncio.Task[None], kind: str) -> None:
            tasks.discard(task)
            (catalog_tasks if kind == "catalog" else profile_tasks).discard(task)

        try:
            while not stop_event.is_set():
                scheduled = False
                kinds = ("profile", "catalog") if prefer_profile else ("catalog", "profile")
                prefer_profile = not prefer_profile
                for kind in kinds:
                    if len(tasks) >= total_limit:
                        break
                    if kind == "catalog":
                        if len(catalog_tasks) >= self._settings.catalog_worker_max_concurrent_jobs:
                            continue
                        async with self._session_factory() as session:
                            claim = await claim_next_catalog_sync_run(
                                session,
                                worker_id=self._worker_id,
                                lease_seconds=self._settings.catalog_worker_lease_seconds,
                                max_attempts=self._settings.catalog_worker_max_attempts,
                            )
                        if claim is None:
                            continue
                        task = asyncio.create_task(
                            self._process_claim(claim),
                            name=f"catalog-discovery-{claim.run_id}",
                        )
                        tasks.add(task)
                        catalog_tasks.add(task)
                        task.add_done_callback(lambda item: _finish(item, "catalog"))
                        scheduled = True
                        continue

                    if len(profile_tasks) >= profile_limit:
                        continue
                    async with self._session_factory() as session:
                        profile_claim = await claim_next_source_profile_run(
                            session,
                            worker_id=self._worker_id,
                            lease_seconds=self._settings.source_profile_worker_lease_seconds,
                            max_attempts=self._settings.source_profile_worker_max_attempts,
                        )
                    if profile_claim is None:
                        continue
                    task = asyncio.create_task(
                        self._process_profile_claim(profile_claim),
                        name=f"source-profile-{profile_claim.run_id}",
                    )
                    tasks.add(task)
                    profile_tasks.add(task)
                    task.add_done_callback(lambda item: _finish(item, "profile"))
                    scheduled = True
                if scheduled and len(tasks) < total_limit:
                    # Fill remaining capacity without waiting a poll interval,
                    # while alternating queues in the next iteration.
                    await asyncio.sleep(0)
                    continue
                if tasks:
                    await asyncio.wait(
                        tasks,
                        timeout=poll_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=poll_interval,
                        )
                    except TimeoutError:
                        pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_claim(self, claim: CatalogSyncLease) -> None:
        try:
            # Prove the claim is still live before decrypting credentials or
            # opening a client connection. This also makes cancellation that
            # arrives just after a claim prevent remote work altogether.
            async with self._session_factory() as session:
                is_current = await renew_catalog_sync_lease(
                    session,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    lease_token=claim.lease_token,
                    lease_seconds=self._settings.catalog_worker_lease_seconds,
                )
            if not is_current:
                raise CatalogLeaseLost()
            context = await self._load_context(claim)
            discovered = await self._discover_with_lease(claim, context)
            snapshot_input = _to_catalog_snapshot(discovered)
            async with self._session_factory() as session:
                await publish_catalog_snapshot(
                    session,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    snapshot_input=snapshot_input,
                    lease_token=claim.lease_token,
                )
        except asyncio.CancelledError:
            # A graceful worker shutdown leaves the lease to expire. A later
            # worker can recover it; no terminal failure is written here.
            raise
        except (CatalogLeaseLost, CatalogRunLeaseLostError, CatalogRunStateError):
            return
        except (CatalogRunSupersededError, CatalogScopeChangedError):
            return
        except Exception as exc:
            failure_code, retryable = _classify_failure(exc)
            await self._record_failure(claim, failure_code=failure_code, retryable=retryable)

    async def _process_profile_claim(self, claim: SourceProfileLease) -> None:
        """Run one manual profile under durable lease/cancellation fencing."""

        try:
            async with self._session_factory() as session:
                current = await renew_source_profile_lease(
                    session,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    lease_token=claim.lease_token,
                    lease_seconds=self._settings.source_profile_worker_lease_seconds,
                )
            if not current:
                raise SourceProfileLeaseLostError()
            async with self._session_factory() as session:
                profile_context = await load_source_profile_execution_context(
                    session,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    lease_token=claim.lease_token,
                )
            connection_context = await self._load_profile_connection_context(claim)
            aggregates = await self._profile_with_lease(
                claim, profile_context, connection_context
            )
            async with self._session_factory() as session:
                published = await publish_source_profile_observations(
                    session,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    lease_token=claim.lease_token,
                    table_aggregates=aggregates,
                )
            if not published:
                return
        except asyncio.CancelledError:
            # Graceful process shutdown leaves the durable lease to expire;
            # it never turns an in-flight remote read into a hidden retry.
            raise
        except (SourceProfileLeaseLostError, SourceProfileSupersededError):
            return
        except Exception as exc:
            failure_code, retryable = _classify_profile_failure(exc)
            await self._record_profile_failure(
                claim, failure_code=failure_code, retryable=retryable
            )

    async def _load_profile_connection_context(
        self, claim: SourceProfileLease
    ) -> ProfileConnectionContext:
        """Decrypt once after a live lease check; never log source settings."""

        async with self._session_factory() as session:
            source = (
                await session.execute(
                    select(DataSource)
                    .where(DataSource.id == claim.source_id)
                    .options(selectinload(DataSource.credential))
                )
            ).scalar_one_or_none()
            if source is None or source.credential is None:
                raise NotFoundError("profile source context not found")
            if not source.profiling_enabled:
                raise SourceProfileSupersededError()
            try:
                bundle = self._protector.decrypt(
                    source_id=source.id,
                    ciphertext=source.credential.ciphertext,
                    key_id=source.credential.key_id,
                    encryption_format=source.credential.encryption_format,
                )
            except (CredentialUnavailableError, InvalidCredentialBundleError):
                raise CatalogWorkerConfigurationError("source credentials are unavailable") from None
            return ProfileConnectionContext(
                source_id=source.id,
                host=source.host,
                port=source.port,
                database_name=source.database_name,
                username=bundle.username,
                password=bundle.password,
                custom_ca_pem=bundle.custom_ca_pem,
            )

    def _profile_request_for_asset(
        self, context: ProfileExecutionContext, asset
    ) -> ProfileRequest:
        columns = tuple(
            ProfiledColumn(
                column_id=column.column_id,
                column_name=column.column_name,
                native_type_schema=column.native_type_schema,
                native_type_name=column.native_type_name,
                native_type_oid=column.native_type_oid,
                native_type_modifier=column.native_type_modifier,
                is_nullable=column.is_nullable,
            )
            for column in asset.columns
        )
        operations: tuple[ProfileOperation, ...] = (ProfileOperation.ROW_COUNT,)
        if columns:
            operations += (ProfileOperation.COLUMN_NULL_DISTINCT,)
        if any(column.scalar_kind == "numeric" for column in asset.columns):
            operations += (ProfileOperation.NUMERIC_SUMMARY,)
        return ProfileRequest(
            relation=ProfiledRelation(asset.schema_name, asset.table_name),
            columns=columns,
            sampling=ProfileSamplingConfig(
                sample_percent=context.sample_percent,
                sample_limit=context.sample_limit,
                repeatable_seed=context.sample_seed,
            ),
            operations=operations,
            budget=ProfileExecutionBudget(
                connect_timeout_seconds=self._settings.postgres_profile_connect_timeout_seconds,
                statement_timeout_seconds=self._settings.postgres_profile_statement_timeout_seconds,
                lock_timeout_seconds=self._settings.postgres_profile_lock_timeout_seconds,
                idle_transaction_timeout_seconds=(
                    self._settings.postgres_profile_idle_transaction_timeout_seconds
                ),
                operation_timeout_seconds=self._settings.postgres_profile_operation_timeout_seconds,
            ),
        )

    async def _profile_all_assets(
        self,
        context: ProfileExecutionContext,
        connection: ProfileConnectionContext,
        cancellation: PostgresExecutionCancellation,
    ) -> tuple[ProfileTableAggregate, ...]:
        adapter = self._profiling_adapter_factory(self._settings)
        results: list[ProfileTableAggregate] = []
        for asset in context.assets:
            request = self._profile_request_for_asset(context, asset)
            result = await adapter.execute(
                host=connection.host,
                port=connection.port,
                database_name=connection.database_name,
                username=connection.username,
                password=connection.password,
                custom_ca_pem=connection.custom_ca_pem,
                request=request,
                cancellation=cancellation,
            )
            if (
                result.sampling_method != "table_sample_system"
                or Decimal(str(result.sample_percent)) != context.sample_percent
                or result.sample_limit != context.sample_limit
                or result.repeatable_seed != context.sample_seed
                or result.measure_coverage != "exact_for_observed_sample"
                or result.observed_row_count < 0
                or result.observed_row_count > context.sample_limit
            ):
                raise PostgresProfilingRequestError()
            expected_columns = {column.column_id: column for column in asset.columns}
            if (
                len(result.columns) != len(expected_columns)
                or {observation.column_id for observation in result.columns} != set(expected_columns)
            ):
                raise PostgresProfilingRequestError()
            aggregates: list[ProfileColumnAggregate] = []
            for observation in result.columns:
                expected = expected_columns[observation.column_id]
                if (
                    observation.status is not ProfileColumnStatus.SUPPORTED
                    or observation.observed_non_null_count is None
                    or observation.observed_null_count is None
                    or observation.observed_distinct_count is None
                ):
                    # Unsupported adapter output must never become a partial
                    # record; selection validation already excluded it.
                    raise PostgresProfilingRequestError()
                warnings = tuple(
                    "numeric_summary_unavailable"
                    if warning == "non_finite_numeric_result"
                    else warning
                    for warning in observation.warning_codes
                )
                numeric = observation.numeric_summary
                if expected.scalar_kind == "numeric":
                    if numeric is None:
                        raise PostgresProfilingRequestError()
                    numeric_count = numeric.observed_non_null_count
                    numeric_sum = numeric.sum_value
                    numeric_minimum = numeric.minimum_value
                    numeric_maximum = numeric.maximum_value
                    numeric_mean = numeric.mean_value
                    if numeric.unavailable_reason is not None:
                        warnings = tuple(dict.fromkeys((*warnings, "numeric_summary_unavailable")))
                else:
                    if numeric is not None:
                        raise PostgresProfilingRequestError()
                    numeric_count = None
                    numeric_sum = None
                    numeric_minimum = None
                    numeric_maximum = None
                    numeric_mean = None
                aggregates.append(
                    ProfileColumnAggregate(
                        column_id=observation.column_id,
                        # The transport supplies exact aggregates for one
                        # bounded CTE population; empty is its own truthful
                        # status, never a complete-table claim.
                        status=(
                            SourceProfileObservationStatus.EMPTY_SAMPLE
                            if result.observed_row_count == 0
                            else SourceProfileObservationStatus.COMPLETE
                        ),
                        observed_non_null_count=observation.observed_non_null_count,
                        observed_null_count=observation.observed_null_count,
                        observed_distinct_count=observation.observed_distinct_count,
                        numeric_count=numeric_count,
                        numeric_sum=numeric_sum,
                        numeric_minimum=numeric_minimum,
                        numeric_maximum=numeric_maximum,
                        numeric_mean=numeric_mean,
                        warning_codes=warnings,
                    )
                )
            results.append(
                ProfileTableAggregate(
                    asset_id=asset.asset_id,
                    observed_row_count=result.observed_row_count,
                    status=(
                        SourceProfileObservationStatus.EMPTY_SAMPLE
                        if result.observed_row_count == 0
                        else SourceProfileObservationStatus.COMPLETE
                    ),
                    columns=tuple(aggregates),
                    collected_at=result.collected_at,
                )
            )
        return tuple(results)

    async def _profile_with_lease(
        self,
        claim: SourceProfileLease,
        context: ProfileExecutionContext,
        connection: ProfileConnectionContext,
    ) -> tuple[ProfileTableAggregate, ...]:
        """Renew while remote work runs; lease loss physically stops I/O."""

        async with self._session_factory() as session:
            current = await renew_source_profile_lease(
                session,
                source_id=claim.source_id,
                run_id=claim.run_id,
                lease_token=claim.lease_token,
                lease_seconds=self._settings.source_profile_worker_lease_seconds,
            )
        if not current:
            raise SourceProfileLeaseLostError()
        cancellation = PostgresExecutionCancellation()
        task = asyncio.create_task(
            self._profile_all_assets(context, connection, cancellation),
            name=f"source-profile-remote-{claim.run_id}",
        )
        interval = min(
            self._settings.source_profile_worker_lease_renewal_seconds,
            self._settings.source_profile_worker_lease_seconds / 2,
        )
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {task}, timeout=interval, return_when=asyncio.FIRST_COMPLETED
                )
                if task in done:
                    return await task
                async with self._session_factory() as session:
                    current = await renew_source_profile_lease(
                        session,
                        source_id=claim.source_id,
                        run_id=claim.run_id,
                        lease_token=claim.lease_token,
                        lease_seconds=self._settings.source_profile_worker_lease_seconds,
                    )
                if not current:
                    await cancellation.cancel()
                    raise SourceProfileLeaseLostError()
        finally:
            if not task.done():
                await cancellation.cancel()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _record_profile_failure(
        self,
        claim: SourceProfileLease,
        *,
        failure_code: str,
        retryable: bool,
    ) -> None:
        delay = (
            min(
                self._settings.source_profile_worker_retry_max_seconds,
                self._settings.source_profile_worker_retry_base_seconds
                * (2 ** max(claim.attempt_count - 1, 0)),
            )
            if retryable
            else None
        )
        try:
            async with self._session_factory() as session:
                await finalize_source_profile_worker_failure(
                    session,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    lease_token=claim.lease_token,
                    failure_code=failure_code,
                    retry_delay_seconds=delay,
                    max_attempts=self._settings.source_profile_worker_max_attempts,
                )
        except SourceProfilePersistenceError:
            logger.warning("source_profile_failure_record_deferred", run_id=str(claim.run_id))

    async def _load_context(self, claim: CatalogSyncLease) -> DiscoveryContext:
        async with self._session_factory() as session:
            source = (
                await session.execute(
                    select(DataSource)
                    .where(DataSource.id == claim.source_id)
                    .options(
                        selectinload(DataSource.credential),
                        selectinload(DataSource.table_scopes),
                    )
                )
            ).scalar_one_or_none()
            if source is None or source.credential is None or not source.table_scopes:
                raise NotFoundError("catalog source context not found")
            try:
                bundle = self._protector.decrypt(
                    source_id=source.id,
                    ciphertext=source.credential.ciphertext,
                    key_id=source.credential.key_id,
                    encryption_format=source.credential.encryption_format,
                )
            except (CredentialUnavailableError, InvalidCredentialBundleError):
                raise CatalogWorkerConfigurationError("source credentials are unavailable") from None
            return DiscoveryContext(
                source_id=source.id,
                host=source.host,
                port=source.port,
                database_name=source.database_name,
                username=bundle.username,
                password=bundle.password,
                custom_ca_pem=bundle.custom_ca_pem,
                selected_tables=tuple(
                    InventoryTable(scope.schema_name, scope.table_name)
                    for scope in sorted(
                        source.table_scopes,
                        key=lambda scope: (scope.schema_name, scope.table_name),
                    )
                ),
            )

    async def _discover_with_lease(
        self,
        claim: CatalogSyncLease,
        context: DiscoveryContext,
    ) -> tuple[DiscoveredTable, ...]:
        discovery = asyncio.create_task(
            self._adapter_factory(self._settings).discover_selected_tables(
                host=context.host,
                port=context.port,
                database_name=context.database_name,
                username=context.username,
                password=context.password,
                custom_ca_pem=context.custom_ca_pem,
                selected_tables=context.selected_tables,
            ),
            name=f"catalog-remote-discovery-{claim.run_id}",
        )
        renewal_interval = min(
            self._settings.catalog_worker_lease_renewal_seconds,
            self._settings.catalog_worker_lease_seconds / 2,
        )
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {discovery},
                    timeout=renewal_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if discovery in done:
                    return await discovery
                async with self._session_factory() as session:
                    renewed = await renew_catalog_sync_lease(
                        session,
                        source_id=claim.source_id,
                        run_id=claim.run_id,
                        lease_token=claim.lease_token,
                        lease_seconds=self._settings.catalog_worker_lease_seconds,
                    )
                if not renewed:
                    raise CatalogLeaseLost()
        finally:
            if not discovery.done():
                discovery.cancel()
                await asyncio.gather(discovery, return_exceptions=True)

    async def _record_failure(
        self,
        claim: CatalogSyncLease,
        *,
        failure_code: str,
        retryable: bool,
    ) -> None:
        delay = (
            min(
                self._settings.catalog_worker_retry_max_seconds,
                self._settings.catalog_worker_retry_base_seconds * (2 ** max(claim.attempt_count - 1, 0)),
            )
            if retryable
            else None
        )
        try:
            async with self._session_factory() as session:
                await finalize_catalog_sync_worker_failure(
                    session,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    lease_token=claim.lease_token,
                    failure_code=failure_code,
                    retry_delay_seconds=delay,
                    max_attempts=self._settings.catalog_worker_max_attempts,
                )
        except CatalogPersistenceError:
            # The lease will expire and be recovered. Do not write raw driver
            # details or connector metadata to logs.
            logger.warning("catalog_worker_failure_record_deferred", run_id=str(claim.run_id))


def _classify_failure(exc: Exception) -> tuple[str, bool]:
    """Return only documented safe codes; never include a driver message."""

    if isinstance(exc, MetadataLimitError):
        return exc.code, False
    if isinstance(exc, PostgresAdapterError) and isinstance(
        exc,
        (PostgresCredentialFailure, MetadataAccessError, SelectedTableUnavailableError, PostgresTLSFailure),
    ):
        return exc.code, False
    if isinstance(exc, CatalogWorkerConfigurationError):
        return "worker_configuration_error", False
    if isinstance(exc, ConnectionPolicyError):
        return "unsupported_configuration", False
    if isinstance(exc, CatalogInputError):
        return "catalog_discovery_invalid", False
    if isinstance(exc, NotFoundError):
        return "source_unavailable", False
    if isinstance(exc, (PostgresTimeoutError, MetadataDiscoveryTimeoutError)):
        return exc.code, True
    if isinstance(exc, PostgresAdapterError):
        return exc.code, True
    if isinstance(exc, CatalogPersistenceError):
        return "catalog_persistence_failed", True
    return "catalog_discovery_failed", True


def _classify_profile_failure(exc: Exception) -> tuple[str, bool]:
    """Map profiling failures to stable safe categories only.

    No remote error string, SQL, hostname, role, or sampled value crosses this
    boundary into durable state or logs.
    """

    if isinstance(exc, (PostgresProfilingRequestError, SourceProfileInputError)):
        return "profiling_request_invalid", False
    if isinstance(exc, PostgresExecutionCancelledError):
        return "execution_cancelled", False
    if isinstance(exc, (PostgresExecutionConfigurationError, ConnectionPolicyError)):
        return "unsupported_configuration", False
    if isinstance(exc, PostgresExecutionSchemaChangedError):
        return "schema_changed", False
    if isinstance(exc, PostgresExecutionAccessChangedError):
        return "access_changed", False
    if isinstance(exc, PostgresExecutionCredentialError):
        return "credential_unavailable", False
    if isinstance(exc, PostgresExecutionTLSFailureError):
        return "tls_verification_failed", False
    if isinstance(exc, PostgresExecutionRestrictedRoleError):
        return "restricted_role_required", False
    if isinstance(exc, PostgresExecutionTimeoutError):
        return "execution_timeout", True
    if isinstance(exc, CatalogWorkerConfigurationError):
        return "worker_configuration_error", False
    if isinstance(exc, NotFoundError):
        return "source_unavailable", False
    if isinstance(exc, SourceProfilePersistenceError):
        return "profile_persistence_failed", True
    if isinstance(exc, PostgresExecutionAdapterError):
        return "profile_execution_failed", True
    return "profile_execution_failed", True


async def _run_main() -> None:
    configure_logging()
    settings = get_settings()
    worker = CatalogDiscoveryWorker(settings=settings, session_factory=get_sessionmaker())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            pass
    await worker.run_forever(stop_event)


def main() -> None:
    try:
        asyncio.run(_run_main())
    except CatalogWorkerConfigurationError:
        # Deliberately fail before claiming work when connectors or credentials
        # are not configured. Existing file-only web startup is unaffected.
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
