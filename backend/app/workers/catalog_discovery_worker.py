"""Durable worker for PostgreSQL structural catalog discovery.

Run this module in a separately supervised process with ``python -m
app.workers.catalog_discovery_worker``. It claims database-backed sync runs;
the FastAPI web process only creates/cancels/reads those rows.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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


AdapterFactory = Callable[[Settings], PostgresOnboardingAdapter]


def _build_adapter(settings: Settings) -> PostgresOnboardingAdapter:
    return PostgresOnboardingAdapter(settings=settings)


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
    """One explicitly started process that consumes durable sync requests."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        adapter_factory: AdapterFactory | None = None,
        worker_id: str | None = None,
    ):
        self._settings = settings
        self._session_factory = session_factory
        self._adapter_factory = adapter_factory or _build_adapter
        self._worker_id = worker_id or _worker_id()
        self._protector = _require_worker_credentials(settings)

    async def run_once(self) -> bool:
        """Claim and process at most one run; useful for a supervisor or test."""

        async with self._session_factory() as session:
            claim = await claim_next_catalog_sync_run(
                session,
                worker_id=self._worker_id,
                lease_seconds=self._settings.catalog_worker_lease_seconds,
                max_attempts=self._settings.catalog_worker_max_attempts,
            )
        if claim is None:
            return False
        await self._process_claim(claim)
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Maintain bounded process-local task concurrency over durable jobs."""

        tasks: set[asyncio.Task[None]] = set()
        try:
            while not stop_event.is_set():
                while (
                    not stop_event.is_set()
                    and len(tasks) < self._settings.catalog_worker_max_concurrent_jobs
                ):
                    async with self._session_factory() as session:
                        claim = await claim_next_catalog_sync_run(
                            session,
                            worker_id=self._worker_id,
                            lease_seconds=self._settings.catalog_worker_lease_seconds,
                            max_attempts=self._settings.catalog_worker_max_attempts,
                        )
                    if claim is None:
                        break
                    task = asyncio.create_task(
                        self._process_claim(claim),
                        name=f"catalog-discovery-{claim.run_id}",
                    )
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                if tasks:
                    await asyncio.wait(
                        tasks,
                        timeout=self._settings.catalog_worker_poll_interval_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=self._settings.catalog_worker_poll_interval_seconds,
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
