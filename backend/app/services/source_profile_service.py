"""Durable, owner-scoped Stage 6A database profiling boundary.

The service never sends SQL or values to a client database.  It persists a
manual request bound to the current selected catalog, leases it to the shared
worker process, and accepts only validated aggregate results from the fixed
profiling adapter.  Source rows, sampled values, SQL, credentials, and driver
messages are deliberately absent from this module's persistence contracts.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Iterable, Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogSnapshot,
    CatalogSyncRun,
    CatalogSyncRunStatus,
    SourceCatalogHead,
)
from app.db.models.source import DataSource, SourceTableScope, SourceType
from app.db.models.source_profile import (
    SourceProfileColumnObservation,
    SourceProfileFinding,
    SourceProfileFindingSeverity,
    SourceProfileFindingType,
    SourceProfileObservationStatus,
    SourceProfilePopulation,
    SourceProfileRun,
    SourceProfileRunAsset,
    SourceProfileRunColumn,
    SourceProfileRunStatus,
    SourceProfileSamplingMethod,
    SourceProfileTableObservation,
)
from app.exceptions.custom_exceptions import AppError, NotFoundError
from app.services.catalog_service import CatalogTableIdentity, canonical_scope_fingerprint


class SourceProfileServiceError(AppError):
    pass


class SourceProfileInputError(SourceProfileServiceError):
    status_code = 422
    code = "invalid_profile_request"

    def __init__(self, message: str = "profile request is invalid"):
        super().__init__(message)


class SourceProfilingDisabledError(SourceProfileServiceError):
    status_code = 409
    code = "profiling_not_enabled"

    def __init__(self):
        super().__init__("profiling must be explicitly enabled for this source")


class SourceProfileCatalogUnavailableError(SourceProfileServiceError):
    status_code = 409
    code = "profile_catalog_unavailable"

    def __init__(self):
        super().__init__("a current compatible source catalog is required for profiling")


class SourceProfileRunStateError(SourceProfileServiceError):
    status_code = 409
    code = "profile_run_state_invalid"

    def __init__(self):
        super().__init__("profile run cannot be changed in its current state")


class SourceProfilePersistenceError(SourceProfileServiceError):
    status_code = 503
    code = "profile_persistence_failed"

    def __init__(self):
        super().__init__("could not complete the profiling request")


class SourceProfileLeaseLostError(SourceProfileServiceError):
    status_code = 409
    code = "profile_run_lease_lost"

    def __init__(self):
        super().__init__("profile worker lease is no longer current")


class SourceProfileSupersededError(SourceProfileServiceError):
    status_code = 409
    code = "profile_run_superseded"

    def __init__(self):
        super().__init__("profile run is no longer compatible with the source")


@dataclass(frozen=True)
class ProfileSelection:
    """One current catalog table and explicitly selected supported columns."""

    asset_id: uuid.UUID
    column_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class SourceProfileLease:
    source_id: uuid.UUID
    run_id: uuid.UUID
    lease_token: uuid.UUID
    attempt_count: int


@dataclass(frozen=True)
class ProfileColumnExecutionContext:
    column_id: uuid.UUID
    column_name: str
    formatted_native_type: str
    native_type_schema: str | None
    native_type_name: str | None
    native_type_oid: int | None
    native_type_modifier: int | None
    is_nullable: bool
    scalar_kind: Literal["numeric", "boolean", "text", "temporal", "uuid"]


@dataclass(frozen=True)
class ProfileAssetExecutionContext:
    asset_id: uuid.UUID
    schema_name: str
    table_name: str
    columns: tuple[ProfileColumnExecutionContext, ...]


@dataclass(frozen=True)
class ProfileExecutionContext:
    source_id: uuid.UUID
    run_id: uuid.UUID
    snapshot_id: uuid.UUID
    sample_percent: Decimal
    sample_limit: int
    sample_seed: int
    max_operations: int
    assets: tuple[ProfileAssetExecutionContext, ...]


@dataclass(frozen=True)
class ProfileColumnAggregate:
    """Validated aggregate-only output. It has no raw-value field."""

    column_id: uuid.UUID
    status: SourceProfileObservationStatus
    observed_non_null_count: int
    observed_null_count: int
    observed_distinct_count: int
    numeric_count: int | None = None
    numeric_sum: Decimal | None = None
    numeric_minimum: Decimal | None = None
    numeric_maximum: Decimal | None = None
    numeric_mean: Decimal | None = None
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileTableAggregate:
    asset_id: uuid.UUID
    observed_row_count: int
    status: SourceProfileObservationStatus
    columns: tuple[ProfileColumnAggregate, ...]
    warning_codes: tuple[str, ...] = ()
    collected_at: datetime | None = None


@dataclass(frozen=True)
class ProfileRunPage:
    source_id: uuid.UUID
    page: int
    page_size: int
    total: int
    runs: tuple[SourceProfileRun, ...]


@dataclass(frozen=True)
class ProfileTableObservationView:
    observation: SourceProfileTableObservation
    asset: CatalogAsset


@dataclass(frozen=True)
class ProfileColumnObservationView:
    observation: SourceProfileColumnObservation
    table_observation: SourceProfileTableObservation
    asset: CatalogAsset
    column: CatalogColumn
    scalar_kind: str


@dataclass(frozen=True)
class ProfileObservationPage:
    source_id: uuid.UUID
    run: SourceProfileRun
    page: int
    page_size: int
    total_tables: int
    table_observations: tuple[ProfileTableObservationView, ...]
    column_observations: tuple[ProfileColumnObservationView, ...]


@dataclass(frozen=True)
class ProfileFindingView:
    finding: SourceProfileFinding
    table_observation: SourceProfileTableObservation
    column_observation: SourceProfileColumnObservation
    asset: CatalogAsset
    column: CatalogColumn


@dataclass(frozen=True)
class ProfileFindingPage:
    source_id: uuid.UUID
    run: SourceProfileRun
    page: int
    page_size: int
    total: int
    findings: tuple[ProfileFindingView, ...]


_SAFE_FAILURE = re.compile(r"^[a-z0-9_]{1,64}$")
_SAFE_WARNINGS = frozenset(
    {
        "sample_empty",
        "sample_limit_reached",
        "system_sampling_block_bias",
        "numeric_summary_unavailable",
        "unsupported_scalar_type",
        "profile_result_incomplete",
    }
)
_SCALAR_KINDS: dict[str, Literal["numeric", "boolean", "text", "temporal", "uuid"]] = {
    "int2": "numeric",
    "int4": "numeric",
    "int8": "numeric",
    "numeric": "numeric",
    "float4": "numeric",
    "float8": "numeric",
    "bool": "boolean",
    "text": "text",
    "varchar": "text",
    "bpchar": "text",
    "date": "temporal",
    "timestamp": "temporal",
    "timestamptz": "temporal",
    "uuid": "uuid",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def _locked_source(
    session: AsyncSession, source_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> DataSource | None:
    statement = select(DataSource).where(DataSource.id == source_id)
    if owner_id is not None:
        if not isinstance(owner_id, uuid.UUID):
            return None
        statement = statement.where(DataSource.owner_user_id == owner_id)
    return (await session.execute(statement.with_for_update())).scalar_one_or_none()


async def _owned_source_or_raise(
    session: AsyncSession, source_id: uuid.UUID, owner_id: uuid.UUID
) -> DataSource:
    # Owner-scoped inspection does not mutate a source or profile run. Keep
    # these reads lock-free so API serialization never holds a source-row lock
    # while an explicit configuration/cancellation change is waiting.
    if not isinstance(owner_id, uuid.UUID):
        await _rollback(session)
        raise NotFoundError("source not found")
    source = (
        await session.execute(
            select(DataSource).where(
                DataSource.id == source_id,
                DataSource.owner_user_id == owner_id,
            )
        )
    ).scalar_one_or_none()
    if source is None:
        await _rollback(session)
        raise NotFoundError("source not found")
    return source


async def _locked_run(
    session: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID
) -> SourceProfileRun | None:
    return (
        await session.execute(
            select(SourceProfileRun)
            .where(SourceProfileRun.source_id == source_id, SourceProfileRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _scope(
    session: AsyncSession, source_id: uuid.UUID, *, lock: bool
) -> tuple[CatalogTableIdentity, ...]:
    statement = (
        select(SourceTableScope.schema_name, SourceTableScope.table_name)
        .where(SourceTableScope.source_id == source_id)
        .order_by(SourceTableScope.schema_name, SourceTableScope.table_name)
    )
    if lock:
        statement = statement.with_for_update()
    return tuple(CatalogTableIdentity(row[0], row[1]) for row in (await session.execute(statement)).all())


async def _head_snapshot(
    session: AsyncSession, source_id: uuid.UUID, *, lock: bool
) -> CatalogSnapshot | None:
    head_statement = select(SourceCatalogHead).where(SourceCatalogHead.source_id == source_id)
    if lock:
        head_statement = head_statement.with_for_update()
    head = (await session.execute(head_statement)).scalar_one_or_none()
    if head is None:
        return None
    statement = select(CatalogSnapshot).where(
        CatalogSnapshot.source_id == source_id, CatalogSnapshot.id == head.snapshot_id
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalar_one_or_none()


def _clear_lease(run: SourceProfileRun) -> None:
    run.lease_owner = None
    run.lease_token = None
    run.lease_expires_at = None


def _expired(value: datetime, now: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now


def _cancel(run: SourceProfileRun, code: str | None) -> None:
    run.cancel_requested_at = run.cancel_requested_at or _now()
    run.status = SourceProfileRunStatus.CANCELLED
    run.completed_at = _now()
    run.failure_code = code
    run.next_attempt_at = None
    _clear_lease(run)


def _scalar_kind(column: CatalogColumn) -> Literal["numeric", "boolean", "text", "temporal", "uuid"] | None:
    if column.native_type_schema != "pg_catalog" or column.native_type_name is None:
        return None
    return _SCALAR_KINDS.get(column.native_type_name)


def _selection_fingerprint(selections: tuple[ProfileSelection, ...]) -> str:
    data = [
        {"asset_id": str(item.asset_id), "column_ids": [str(value) for value in item.column_ids]}
        for item in selections
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _normalize_selections(values: Iterable[ProfileSelection]) -> tuple[ProfileSelection, ...]:
    try:
        items = tuple(values)
    except TypeError as exc:
        raise SourceProfileInputError() from exc
    if not items or any(not isinstance(item, ProfileSelection) for item in items):
        raise SourceProfileInputError()
    asset_ids: set[uuid.UUID] = set()
    normalized: list[ProfileSelection] = []
    for item in items:
        if not isinstance(item.asset_id, uuid.UUID) or item.asset_id in asset_ids:
            raise SourceProfileInputError()
        asset_ids.add(item.asset_id)
        try:
            columns = tuple(item.column_ids)
        except TypeError as exc:
            raise SourceProfileInputError() from exc
        if any(not isinstance(value, uuid.UUID) for value in columns) or len(set(columns)) != len(columns):
            raise SourceProfileInputError()
        normalized.append(ProfileSelection(item.asset_id, tuple(sorted(columns, key=str))))
    return tuple(sorted(normalized, key=lambda item: str(item.asset_id)))


async def _binding_invalid_reason(
    session: AsyncSession, source: DataSource, run: SourceProfileRun
) -> str | None:
    if source.source_type is not SourceType.POSTGRESQL:
        return "source_unavailable"
    if not source.profiling_enabled:
        return "profiling_disabled"
    if source.profiling_generation != run.config_generation:
        return "profiling_changed"
    if run.cancel_requested_at is not None:
        return "cancelled"
    current_scope = await _scope(session, source.id, lock=True)
    if not current_scope or canonical_scope_fingerprint(current_scope) != run.scope_fingerprint:
        return "scope_changed"
    snapshot = await _head_snapshot(session, source.id, lock=True)
    if snapshot is None or snapshot.id != run.snapshot_id or snapshot.scope_fingerprint != run.scope_fingerprint:
        return "catalog_changed"
    catalog_run = (
        await session.execute(
            select(CatalogSyncRun).where(
                CatalogSyncRun.source_id == source.id, CatalogSyncRun.id == snapshot.sync_run_id
            )
        )
    ).scalar_one_or_none()
    return None if catalog_run is not None and catalog_run.status is CatalogSyncRunStatus.SUCCEEDED else "catalog_changed"


async def configure_source_profiling_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    profiling_enabled: bool,
) -> DataSource:
    """Set explicit opt-in/out without scheduling work.

    Disabling increments the source generation and cancels active runs.  A
    worker that already opened a client connection loses renewal/publication
    rights and will terminate that connection; completed observations remain
    retained but are scope-filtered on all reads.
    """

    if not isinstance(profiling_enabled, bool):
        raise SourceProfileInputError("profiling setting is invalid")
    try:
        source = await _locked_source(session, source_id, owner_user_id)
        if source is None:
            await _rollback(session)
            raise NotFoundError("source not found")
        if source.profiling_enabled != profiling_enabled:
            source.profiling_enabled = profiling_enabled
            source.profiling_generation += 1
        if not profiling_enabled:
            active = (
                await session.execute(
                    select(SourceProfileRun)
                    .where(
                        SourceProfileRun.source_id == source.id,
                        SourceProfileRun.status.in_(
                            (SourceProfileRunStatus.QUEUED, SourceProfileRunStatus.RUNNING)
                        ),
                    )
                    .with_for_update()
                )
            ).scalars().all()
            for run in active:
                _cancel(run, "profiling_disabled")
        await _commit(session)
        return source
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def request_source_profile_run_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    selections: Iterable[ProfileSelection],
    settings: Settings,
) -> SourceProfileRun:
    """Capture a manual profile against the one current compatible catalog.

    Only catalog IDs may arrive from the client.  The database query joins the
    current `SourceTableScope`, so a selection can narrow but never expand the
    saved source scope.  No remote work happens in this request transaction.
    """

    requested = _normalize_selections(selections)
    if len(requested) > settings.postgres_profile_max_tables_per_run:
        raise SourceProfileInputError("profile table limit exceeded")
    try:
        source = await _locked_source(session, source_id, owner_user_id)
        if source is None:
            await _rollback(session)
            raise NotFoundError("source not found")
        if not source.profiling_enabled:
            await _rollback(session)
            raise SourceProfilingDisabledError()
        current_scope = await _scope(session, source.id, lock=True)
        if not current_scope:
            await _rollback(session)
            raise SourceProfileCatalogUnavailableError()
        scope_fingerprint = canonical_scope_fingerprint(current_scope)
        snapshot = await _head_snapshot(session, source.id, lock=True)
        if snapshot is None or snapshot.scope_fingerprint != scope_fingerprint:
            await _rollback(session)
            raise SourceProfileCatalogUnavailableError()
        sync_run = (
            await session.execute(
                select(CatalogSyncRun).where(
                    CatalogSyncRun.source_id == source.id,
                    CatalogSyncRun.id == snapshot.sync_run_id,
                )
            )
        ).scalar_one_or_none()
        if sync_run is None or sync_run.status is not CatalogSyncRunStatus.SUCCEEDED:
            await _rollback(session)
            raise SourceProfileCatalogUnavailableError()

        requested_assets = tuple(item.asset_id for item in requested)
        assets = (
            await session.execute(
                select(CatalogAsset)
                .join(
                    SourceTableScope,
                    and_(
                        SourceTableScope.source_id == CatalogAsset.source_id,
                        SourceTableScope.schema_name == CatalogAsset.schema_name,
                        SourceTableScope.table_name == CatalogAsset.table_name,
                    ),
                )
                .where(
                    CatalogAsset.source_id == source.id,
                    CatalogAsset.snapshot_id == snapshot.id,
                    CatalogAsset.id.in_(requested_assets),
                )
            )
        ).scalars().all()
        assets_by_id = {asset.id: asset for asset in assets}
        if len(assets_by_id) != len(requested):
            await _rollback(session)
            raise SourceProfileInputError("profile selection is outside the current source scope")

        requested_column_ids = tuple(
            column_id for item in requested for column_id in item.column_ids
        )
        if len(requested_column_ids) > settings.postgres_profile_max_columns_per_run:
            await _rollback(session)
            raise SourceProfileInputError("profile column limit exceeded")
        columns_by_asset: dict[uuid.UUID, dict[uuid.UUID, CatalogColumn]] = {
            item.asset_id: {} for item in requested
        }
        if requested_column_ids:
            columns = (
                await session.execute(
                    select(CatalogColumn).where(
                        CatalogColumn.source_id == source.id,
                        CatalogColumn.snapshot_id == snapshot.id,
                        CatalogColumn.asset_id.in_(requested_assets),
                        CatalogColumn.id.in_(requested_column_ids),
                    )
                )
            ).scalars().all()
            for column in columns:
                columns_by_asset[column.asset_id][column.id] = column

        selected_columns: list[tuple[uuid.UUID, CatalogColumn, str]] = []
        operation_count = len(requested)  # one row-count measure per table
        for item in requested:
            if len(item.column_ids) > settings.postgres_profile_max_columns_per_table:
                await _rollback(session)
                raise SourceProfileInputError("profile column limit exceeded")
            resolved = columns_by_asset[item.asset_id]
            if len(resolved) != len(item.column_ids):
                await _rollback(session)
                raise SourceProfileInputError("profile column is outside the selected table")
            for column_id in item.column_ids:
                column = resolved[column_id]
                kind = _scalar_kind(column)
                if kind is None:
                    await _rollback(session)
                    raise SourceProfileInputError("profile column type is unsupported")
                selected_columns.append((item.asset_id, column, kind))
                # NULL + DISTINCT for every scalar; numeric summary adds one
                # fixed logical operation, even though adapter aggregates it
                # in the shared bounded CTE statement.
                operation_count += 3 if kind == "numeric" else 2
        if operation_count > settings.postgres_profile_max_operations_per_run:
            await _rollback(session)
            raise SourceProfileInputError("profile operation budget exceeded")

        selection_fingerprint = _selection_fingerprint(requested)
        active = (
            await session.execute(
                select(SourceProfileRun)
                .where(
                    SourceProfileRun.source_id == source.id,
                    SourceProfileRun.status.in_(
                        (SourceProfileRunStatus.QUEUED, SourceProfileRunStatus.RUNNING)
                    ),
                )
                .order_by(SourceProfileRun.requested_at.desc(), SourceProfileRun.id)
                .with_for_update()
            )
        ).scalars().all()
        for previous in active:
            if (
                previous.snapshot_id == snapshot.id
                and previous.scope_fingerprint == scope_fingerprint
                and previous.selection_fingerprint == selection_fingerprint
                and previous.config_generation == source.profiling_generation
            ):
                await _commit(session)
                return previous
            _cancel(previous, "superseded")

        run = SourceProfileRun(
            id=uuid.uuid4(),
            source_id=source.id,
            requesting_user_id=owner_user_id,
            snapshot_id=snapshot.id,
            scope_fingerprint=scope_fingerprint,
            selection_fingerprint=selection_fingerprint,
            scope_count=len(current_scope),
            selected_asset_count=len(requested),
            selected_column_count=len(selected_columns),
            config_generation=source.profiling_generation,
            sampling_method=SourceProfileSamplingMethod.SYSTEM_TABLE_SAMPLE,
            sample_percent=Decimal(str(settings.postgres_profile_sample_percent)),
            sample_limit=settings.postgres_profile_sample_limit,
            # Generated server-side only; persistent sampling provenance lets
            # an owner understand the exact fixed REPEATABLE configuration.
            sample_seed=secrets.randbelow(2_147_483_648),
            max_operations=operation_count,
            status=SourceProfileRunStatus.QUEUED,
            requested_at=_now(),
        )
        session.add(run)
        for item in requested:
            session.add(
                SourceProfileRunAsset(
                    id=uuid.uuid4(),
                    source_id=source.id,
                    run_id=run.id,
                    snapshot_id=snapshot.id,
                    asset_id=item.asset_id,
                    selected_column_count=len(item.column_ids),
                )
            )
        for asset_id, column, kind in selected_columns:
            session.add(
                SourceProfileRunColumn(
                    id=uuid.uuid4(),
                    source_id=source.id,
                    run_id=run.id,
                    snapshot_id=snapshot.id,
                    asset_id=asset_id,
                    column_id=column.id,
                    scalar_kind=kind,
                )
            )
        await _commit(session)
        return run
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


def _claimable(now: datetime):
    return or_(
        and_(
            SourceProfileRun.status == SourceProfileRunStatus.QUEUED,
            or_(
                SourceProfileRun.next_attempt_at.is_(None),
                SourceProfileRun.next_attempt_at <= now,
            ),
        ),
        and_(
            SourceProfileRun.status == SourceProfileRunStatus.RUNNING,
            or_(
                SourceProfileRun.lease_expires_at.is_(None),
                SourceProfileRun.lease_expires_at <= now,
            ),
        ),
    )


async def claim_next_source_profile_run(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: float,
    max_attempts: int,
) -> SourceProfileLease | None:
    """Claim one durable profile job under the same source-before-run lock order."""

    if (
        not isinstance(worker_id, str)
        or not worker_id
        or "\x00" in worker_id
        or len(worker_id.encode()) > 128
        or lease_seconds <= 0
        or max_attempts < 1
    ):
        raise SourceProfileInputError("profile worker claim is invalid")
    now = _now()
    try:
        candidate = (
            await session.execute(
                select(DataSource.id)
                .join(SourceProfileRun, SourceProfileRun.source_id == DataSource.id)
                .where(
                    _claimable(now),
                    DataSource.profiling_enabled.is_(True),
                    SourceProfileRun.config_generation == DataSource.profiling_generation,
                )
                .order_by(SourceProfileRun.requested_at, SourceProfileRun.id)
                .with_for_update(of=DataSource, skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if candidate is None:
            await _commit(session)
            return None
        source = await _locked_source(session, candidate)
        if source is None:
            await _rollback(session)
            return None
        run = (
            await session.execute(
                select(SourceProfileRun)
                .where(SourceProfileRun.source_id == source.id, _claimable(now))
                .order_by(SourceProfileRun.requested_at, SourceProfileRun.id)
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            await _commit(session)
            return None
        invalid_reason = await _binding_invalid_reason(session, source, run)
        if invalid_reason is not None:
            _cancel(run, invalid_reason)
            await _commit(session)
            return None
        if run.attempt_count >= max_attempts:
            run.status = SourceProfileRunStatus.FAILED
            run.completed_at = now
            run.failure_code = "retry_exhausted"
            run.next_attempt_at = None
            _clear_lease(run)
            await _commit(session)
            return None
        token = uuid.uuid4()
        run.status = SourceProfileRunStatus.RUNNING
        run.started_at = run.started_at or now
        run.completed_at = None
        run.failure_code = None
        run.attempt_count += 1
        run.lease_owner = worker_id
        run.lease_token = token
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        run.next_attempt_at = None
        await _commit(session)
        return SourceProfileLease(source.id, run.id, token, run.attempt_count)
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def renew_source_profile_lease(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_seconds: float,
) -> bool:
    if lease_seconds <= 0:
        raise SourceProfileInputError("profile lease duration is invalid")
    now = _now()
    try:
        source = await _locked_source(session, source_id)
        run = await _locked_run(session, source_id, run_id) if source else None
        if (
            source is None
            or run is None
            or run.status is not SourceProfileRunStatus.RUNNING
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or _expired(run.lease_expires_at, now)
        ):
            await _rollback(session)
            return False
        invalid_reason = await _binding_invalid_reason(session, source, run)
        if invalid_reason is not None:
            _cancel(run, invalid_reason)
            await _commit(session)
            return False
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await _commit(session)
        return True
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def load_source_profile_execution_context(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    lease_token: uuid.UUID,
) -> ProfileExecutionContext:
    """Load one fenced catalog selection before remote I/O begins.

    The source/scope/head checks happen while holding the source row lock, but
    the transaction is committed before a worker can open the remote client
    connection. Publication repeats the same checks, so the short read does
    not create a stale-authority window.
    """

    try:
        source = await _locked_source(session, source_id)
        run = await _locked_run(session, source_id, run_id) if source else None
        if (
            source is None
            or run is None
            or run.status is not SourceProfileRunStatus.RUNNING
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or _expired(run.lease_expires_at, _now())
        ):
            await _rollback(session)
            raise SourceProfileLeaseLostError()
        invalid_reason = await _binding_invalid_reason(session, source, run)
        if invalid_reason is not None:
            _cancel(run, invalid_reason)
            await _commit(session)
            raise SourceProfileSupersededError()
        assets = (
            await session.execute(
                select(SourceProfileRunAsset, CatalogAsset)
                .join(
                    CatalogAsset,
                    and_(
                        CatalogAsset.source_id == SourceProfileRunAsset.source_id,
                        CatalogAsset.snapshot_id == SourceProfileRunAsset.snapshot_id,
                        CatalogAsset.id == SourceProfileRunAsset.asset_id,
                    ),
                )
                .where(
                    SourceProfileRunAsset.source_id == source.id,
                    SourceProfileRunAsset.run_id == run.id,
                    SourceProfileRunAsset.snapshot_id == run.snapshot_id,
                )
                .order_by(CatalogAsset.schema_name, CatalogAsset.table_name, CatalogAsset.id)
            )
        ).all()
        selections = (
            await session.execute(
                select(SourceProfileRunColumn, CatalogColumn)
                .join(
                    CatalogColumn,
                    and_(
                        CatalogColumn.source_id == SourceProfileRunColumn.source_id,
                        CatalogColumn.snapshot_id == SourceProfileRunColumn.snapshot_id,
                        CatalogColumn.asset_id == SourceProfileRunColumn.asset_id,
                        CatalogColumn.id == SourceProfileRunColumn.column_id,
                    ),
                )
                .where(
                    SourceProfileRunColumn.source_id == source.id,
                    SourceProfileRunColumn.run_id == run.id,
                    SourceProfileRunColumn.snapshot_id == run.snapshot_id,
                )
                .order_by(SourceProfileRunColumn.asset_id, CatalogColumn.ordinal_position, CatalogColumn.id)
            )
        ).all()
        columns_by_asset: dict[uuid.UUID, list[ProfileColumnExecutionContext]] = {}
        for selected, column in selections:
            kind = _scalar_kind(column)
            if kind is None or kind != selected.scalar_kind:
                _cancel(run, "catalog_changed")
                await _commit(session)
                raise SourceProfileSupersededError()
            columns_by_asset.setdefault(selected.asset_id, []).append(
                ProfileColumnExecutionContext(
                    column_id=column.id,
                    column_name=column.column_name,
                    formatted_native_type=column.formatted_native_type,
                    native_type_schema=column.native_type_schema,
                    native_type_name=column.native_type_name,
                    native_type_oid=column.native_type_oid,
                    native_type_modifier=column.native_type_modifier,
                    is_nullable=column.is_nullable,
                    scalar_kind=kind,
                )
            )
        if (
            len(assets) != run.selected_asset_count
            or len(selections) != run.selected_column_count
            or any(
                len(columns_by_asset.get(selected.asset_id, ()))
                != selected.selected_column_count
                for selected, _asset in assets
            )
        ):
            _cancel(run, "catalog_changed")
            await _commit(session)
            raise SourceProfileSupersededError()
        contexts = tuple(
            ProfileAssetExecutionContext(
                asset_id=asset.id,
                schema_name=asset.schema_name,
                table_name=asset.table_name,
                columns=tuple(columns_by_asset.get(asset.id, ())),
            )
            for _selected, asset in assets
        )
        await _commit(session)
        return ProfileExecutionContext(
            source_id=source.id,
            run_id=run.id,
            snapshot_id=run.snapshot_id,
            sample_percent=Decimal(run.sample_percent),
            sample_limit=run.sample_limit,
            sample_seed=run.sample_seed,
            max_operations=run.max_operations,
            assets=contexts,
        )
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


def _safe_warnings(values: Iterable[str]) -> tuple[str, ...]:
    try:
        result = tuple(dict.fromkeys(values))
    except TypeError as exc:
        raise SourceProfileInputError("profile warnings are invalid") from exc
    if any(not isinstance(value, str) or value not in _SAFE_WARNINGS for value in result):
        raise SourceProfileInputError("profile warnings are invalid")
    return result


def _finite_decimal(value: Decimal | None, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise SourceProfileInputError(f"profile {field} is invalid")
    try:
        # PostgreSQL's aggregate can legitimately fit the persisted
        # NUMERIC(38, 10) contract while exceeding Python's default Decimal
        # context precision (28).  Quantize under a deliberately local,
        # bounded context so a valid high-magnitude aggregate is preserved
        # rather than falsely turning the entire profile into a failure.
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            value = value.quantize(Decimal("0.0000000001"))
    except (InvalidOperation, ValueError) as exc:
        raise SourceProfileInputError(f"profile {field} is invalid") from exc
    if len(value.as_tuple().digits) > 38:
        raise SourceProfileInputError(f"profile {field} is out of range")
    return value


def _validate_table_aggregate(
    result: ProfileTableAggregate, expected_columns: dict[uuid.UUID, str]
) -> tuple[ProfileColumnAggregate, ...]:
    if (
        not isinstance(result, ProfileTableAggregate)
        or not isinstance(result.asset_id, uuid.UUID)
        or isinstance(result.observed_row_count, bool)
        or not isinstance(result.observed_row_count, int)
        or result.observed_row_count < 0
        or result.status
        not in (SourceProfileObservationStatus.COMPLETE, SourceProfileObservationStatus.EMPTY_SAMPLE)
    ):
        raise SourceProfileInputError("profile result is invalid")
    _safe_warnings(result.warning_codes)
    if (result.observed_row_count == 0) != (result.status is SourceProfileObservationStatus.EMPTY_SAMPLE):
        raise SourceProfileInputError("profile population status is invalid")
    try:
        columns = tuple(result.columns)
    except TypeError as exc:
        raise SourceProfileInputError("profile columns do not match the request") from exc
    if any(not isinstance(item, ProfileColumnAggregate) for item in columns):
        raise SourceProfileInputError("profile column result is invalid")
    if (
        len(columns) != len(expected_columns)
        or len({item.column_id for item in columns}) != len(columns)
        or {item.column_id for item in columns} != set(expected_columns)
    ):
        raise SourceProfileInputError("profile columns do not match the request")
    for item in columns:
        if (
            item.status
            not in (SourceProfileObservationStatus.COMPLETE, SourceProfileObservationStatus.EMPTY_SAMPLE)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (
                    item.observed_non_null_count,
                    item.observed_null_count,
                    item.observed_distinct_count,
                )
            )
            or item.observed_non_null_count + item.observed_null_count != result.observed_row_count
            or item.observed_distinct_count > item.observed_non_null_count
            or ((result.observed_row_count == 0) != (item.status is SourceProfileObservationStatus.EMPTY_SAMPLE))
        ):
            raise SourceProfileInputError("profile column result is invalid")
        _safe_warnings(item.warning_codes)
        kind = expected_columns[item.column_id]
        numeric_values = (
            item.numeric_count,
            item.numeric_sum,
            item.numeric_minimum,
            item.numeric_maximum,
            item.numeric_mean,
        )
        if kind != "numeric" and any(value is not None for value in numeric_values):
            raise SourceProfileInputError("numeric result is invalid for this column")
        if kind == "numeric":
            if (
                item.numeric_count is None
                or isinstance(item.numeric_count, bool)
                or not isinstance(item.numeric_count, int)
                or item.numeric_count != item.observed_non_null_count
            ):
                raise SourceProfileInputError("numeric profile count is invalid")
            numeric_summary = (
                ("sum", item.numeric_sum),
                ("minimum", item.numeric_minimum),
                ("maximum", item.numeric_maximum),
                ("mean", item.numeric_mean),
            )
            present_values = [value for _label, value in numeric_summary if value is not None]
            if present_values and len(present_values) != len(numeric_summary):
                raise SourceProfileInputError("numeric profile summary is incomplete")
            if item.numeric_count == 0 and present_values:
                raise SourceProfileInputError("numeric profile summary is invalid")
            validated_numeric = {
                label: _finite_decimal(value, label) for label, value in numeric_summary
            }
            if present_values and not (
                validated_numeric["minimum"]
                <= validated_numeric["mean"]
                <= validated_numeric["maximum"]
            ):
                raise SourceProfileInputError("numeric profile summary is invalid")
    return columns


def _findings_for_column(
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    table_observation: SourceProfileTableObservation,
    column_observation: SourceProfileColumnObservation,
) -> tuple[SourceProfileFinding, ...]:
    """Create only deterministic claims qualified to the observed population."""

    if (
        table_observation.status is not SourceProfileObservationStatus.COMPLETE
        or column_observation.status is not SourceProfileObservationStatus.COMPLETE
        or table_observation.observed_row_count <= 0
    ):
        return ()
    limitations = (
        "Exact for the bounded observed sample only. PostgreSQL SYSTEM sampling is block based; "
        "this does not prove a whole-table property."
    )
    rows: list[SourceProfileFinding] = []
    null_rate = Decimal(column_observation.null_count) / Decimal(table_observation.observed_row_count)
    if column_observation.null_count:
        percentage = (null_rate * Decimal("100")).quantize(Decimal("0.01"))
        rows.append(
            SourceProfileFinding(
                id=uuid.uuid4(),
                source_id=source_id,
                run_id=run_id,
                table_observation_id=table_observation.id,
                column_observation_id=column_observation.id,
                finding_type=SourceProfileFindingType.OBSERVED_NULL_RATE,
                severity=(
                    SourceProfileFindingSeverity.MEDIUM
                    if null_rate >= Decimal("0.10")
                    else SourceProfileFindingSeverity.LOW
                ),
                title="Observed null values",
                description=f"{percentage}% of observed rows had a null value.",
                limitations=limitations,
                evidence_json={
                    "population": SourceProfilePopulation.OBSERVED_SAMPLE.value,
                    "observed_row_count": table_observation.observed_row_count,
                    "null_count": column_observation.null_count,
                    "null_rate": str(null_rate),
                },
                generated_at=_now(),
            )
        )
    if column_observation.non_null_count and column_observation.distinct_count == 1:
        rows.append(
            SourceProfileFinding(
                id=uuid.uuid4(),
                source_id=source_id,
                run_id=run_id,
                table_observation_id=table_observation.id,
                column_observation_id=column_observation.id,
                finding_type=SourceProfileFindingType.OBSERVED_CONSTANT_NON_NULL,
                severity=SourceProfileFindingSeverity.LOW,
                title="One non-null value observed",
                description="Only one distinct non-null value occurred in the observed sample.",
                limitations=limitations,
                evidence_json={
                    "population": SourceProfilePopulation.OBSERVED_SAMPLE.value,
                    "observed_row_count": table_observation.observed_row_count,
                    "non_null_count": column_observation.non_null_count,
                    "distinct_count": column_observation.distinct_count,
                },
                generated_at=_now(),
            )
        )
    return tuple(rows)


async def publish_source_profile_observations(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    lease_token: uuid.UUID,
    table_aggregates: Iterable[ProfileTableAggregate],
) -> bool:
    """Persist aggregate-only output if the lease/catalog/config are still current.

    Publication does not accept a row, SQL string, raw value, arbitrary JSON,
    or error message.  A false return means the caller lost its fence and must
    discard remote results rather than trying to publish them later.
    """

    try:
        aggregates = tuple(table_aggregates)
    except TypeError as exc:
        raise SourceProfileInputError("profile result is invalid") from exc
    try:
        source = await _locked_source(session, source_id)
        run = await _locked_run(session, source_id, run_id) if source else None
        if (
            source is None
            or run is None
            or run.status is not SourceProfileRunStatus.RUNNING
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or _expired(run.lease_expires_at, _now())
        ):
            await _rollback(session)
            return False
        invalid_reason = await _binding_invalid_reason(session, source, run)
        if invalid_reason is not None:
            _cancel(run, invalid_reason)
            await _commit(session)
            return False
        run_assets = (
            await session.execute(
                select(SourceProfileRunAsset).where(
                    SourceProfileRunAsset.source_id == source.id,
                    SourceProfileRunAsset.run_id == run.id,
                    SourceProfileRunAsset.snapshot_id == run.snapshot_id,
                )
            )
        ).scalars().all()
        run_columns = (
            await session.execute(
                select(SourceProfileRunColumn).where(
                    SourceProfileRunColumn.source_id == source.id,
                    SourceProfileRunColumn.run_id == run.id,
                    SourceProfileRunColumn.snapshot_id == run.snapshot_id,
                )
            )
        ).scalars().all()
        expected_assets = {item.asset_id for item in run_assets}
        if (
            len(aggregates) != len(expected_assets)
            or {item.asset_id for item in aggregates if isinstance(item, ProfileTableAggregate)}
            != expected_assets
        ):
            await _rollback(session)
            raise SourceProfileInputError("profile results do not match selected tables")
        expected_columns: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
        for item in run_columns:
            expected_columns.setdefault(item.asset_id, {})[item.column_id] = item.scalar_kind

        table_rows: dict[uuid.UUID, SourceProfileTableObservation] = {}
        validated: list[tuple[ProfileTableAggregate, tuple[ProfileColumnAggregate, ...]]] = []
        for aggregate in sorted(aggregates, key=lambda item: str(item.asset_id)):
            columns = _validate_table_aggregate(
                aggregate, expected_columns.get(aggregate.asset_id, {})
            )
            if aggregate.observed_row_count > run.sample_limit:
                raise SourceProfileInputError("profile population exceeds the configured sample limit")
            collected_at = aggregate.collected_at or _now()
            if not isinstance(collected_at, datetime):
                raise SourceProfileInputError("profile collection time is invalid")
            if collected_at.tzinfo is None:
                collected_at = collected_at.replace(tzinfo=timezone.utc)
            warning_codes = list(_safe_warnings(aggregate.warning_codes))
            for code in (
                "system_sampling_block_bias",
                "sample_empty" if aggregate.observed_row_count == 0 else None,
                "sample_limit_reached" if aggregate.observed_row_count >= run.sample_limit else None,
            ):
                if code is not None and code not in warning_codes:
                    warning_codes.append(code)
            row = SourceProfileTableObservation(
                id=uuid.uuid4(),
                source_id=source.id,
                run_id=run.id,
                snapshot_id=run.snapshot_id,
                asset_id=aggregate.asset_id,
                collected_at=collected_at,
                sampling_method=SourceProfileSamplingMethod.SYSTEM_TABLE_SAMPLE,
                sample_percent=run.sample_percent,
                sample_limit=run.sample_limit,
                sample_seed=run.sample_seed,
                population=SourceProfilePopulation.OBSERVED_SAMPLE,
                observed_row_count=aggregate.observed_row_count,
                status=aggregate.status,
                warning_codes=warning_codes,
            )
            session.add(row)
            table_rows[aggregate.asset_id] = row
            validated.append((aggregate, columns))
        await session.flush()

        for aggregate, columns in validated:
            table_row = table_rows[aggregate.asset_id]
            for item in columns:
                warning_codes = list(_safe_warnings(item.warning_codes))
                if item.numeric_count is not None and item.numeric_sum is None and item.numeric_count:
                    if "numeric_summary_unavailable" not in warning_codes:
                        warning_codes.append("numeric_summary_unavailable")
                if aggregate.observed_row_count == 0 and "sample_empty" not in warning_codes:
                    warning_codes.append("sample_empty")
                observation = SourceProfileColumnObservation(
                    id=uuid.uuid4(),
                    source_id=source.id,
                    run_id=run.id,
                    snapshot_id=run.snapshot_id,
                    asset_id=aggregate.asset_id,
                    column_id=item.column_id,
                    table_observation_id=table_row.id,
                    collected_at=table_row.collected_at,
                    population=SourceProfilePopulation.OBSERVED_SAMPLE,
                    observed_row_count=aggregate.observed_row_count,
                    null_count=item.observed_null_count,
                    non_null_count=item.observed_non_null_count,
                    null_rate=(
                        Decimal(item.observed_null_count) / Decimal(aggregate.observed_row_count)
                        if aggregate.observed_row_count
                        else None
                    ),
                    distinct_count=item.observed_distinct_count,
                    numeric_count=item.numeric_count,
                    numeric_sum=_finite_decimal(item.numeric_sum, "sum"),
                    numeric_minimum=_finite_decimal(item.numeric_minimum, "minimum"),
                    numeric_maximum=_finite_decimal(item.numeric_maximum, "maximum"),
                    numeric_mean=_finite_decimal(item.numeric_mean, "mean"),
                    status=item.status,
                    warning_codes=warning_codes,
                )
                session.add(observation)
                await session.flush()
                for finding in _findings_for_column(
                    source_id=source.id,
                    run_id=run.id,
                    table_observation=table_row,
                    column_observation=observation,
                ):
                    session.add(finding)
        run.status = SourceProfileRunStatus.SUCCEEDED
        run.completed_at = _now()
        run.failure_code = None
        run.next_attempt_at = None
        _clear_lease(run)
        await _commit(session)
        return True
    except SourceProfileInputError:
        await _rollback(session)
        raise
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def finalize_source_profile_worker_failure(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    lease_token: uuid.UUID,
    failure_code: str,
    retry_delay_seconds: float | None,
    max_attempts: int,
) -> bool:
    if (
        not isinstance(failure_code, str)
        or _SAFE_FAILURE.fullmatch(failure_code) is None
        or max_attempts < 1
        or (retry_delay_seconds is not None and retry_delay_seconds < 0)
    ):
        raise SourceProfileInputError("profile failure settings are invalid")
    now = _now()
    try:
        source = await _locked_source(session, source_id)
        run = await _locked_run(session, source_id, run_id) if source else None
        if (
            source is None
            or run is None
            or run.status is not SourceProfileRunStatus.RUNNING
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or _expired(run.lease_expires_at, now)
        ):
            await _rollback(session)
            return False
        invalid_reason = await _binding_invalid_reason(session, source, run)
        if invalid_reason is not None:
            _cancel(run, invalid_reason)
            await _commit(session)
            return False
        _clear_lease(run)
        run.failure_code = failure_code
        if retry_delay_seconds is not None and run.attempt_count < max_attempts:
            run.status = SourceProfileRunStatus.QUEUED
            run.next_attempt_at = now + timedelta(seconds=retry_delay_seconds)
            run.completed_at = None
        else:
            run.status = SourceProfileRunStatus.FAILED
            run.next_attempt_at = None
            run.completed_at = now
        await _commit(session)
        return True
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def cancel_source_profile_run_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> SourceProfileRun:
    try:
        source = await _locked_source(session, source_id, owner_user_id)
        run = await _locked_run(session, source_id, run_id) if source else None
        if source is None or run is None:
            await _rollback(session)
            raise NotFoundError("profile run not found")
        if run.status not in (SourceProfileRunStatus.QUEUED, SourceProfileRunStatus.RUNNING):
            await _rollback(session)
            raise SourceProfileRunStateError()
        _cancel(run, None)
        await _commit(session)
        return run
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def get_source_profile_run_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> SourceProfileRun:
    try:
        await _owned_source_or_raise(session, source_id, owner_user_id)
        run = (
            await session.execute(
                select(SourceProfileRun).where(
                    SourceProfileRun.source_id == source_id, SourceProfileRun.id == run_id
                )
            )
        ).scalar_one_or_none()
        if run is None:
            await _rollback(session)
            raise NotFoundError("profile run not found")
        return run
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def list_source_profile_runs_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    page: int,
    page_size: int,
) -> ProfileRunPage:
    try:
        await _owned_source_or_raise(session, source_id, owner_user_id)
        total = (
            await session.execute(
                select(func.count()).select_from(SourceProfileRun).where(
                    SourceProfileRun.source_id == source_id
                )
            )
        ).scalar_one()
        runs = (
            await session.execute(
                select(SourceProfileRun)
                .where(SourceProfileRun.source_id == source_id)
                .order_by(SourceProfileRun.requested_at.desc(), SourceProfileRun.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return ProfileRunPage(source_id, page, page_size, total, tuple(runs))
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def list_source_profile_observations_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    page: int,
    page_size: int,
) -> ProfileObservationPage:
    """Return only artifacts whose table remains in the saved current scope."""

    try:
        run = await get_source_profile_run_for_owner(
            session,
            source_id=source_id,
            run_id=run_id,
            owner_user_id=owner_user_id,
        )
        statement = (
            select(SourceProfileTableObservation, CatalogAsset)
            .join(
                CatalogAsset,
                and_(
                    CatalogAsset.source_id == SourceProfileTableObservation.source_id,
                    CatalogAsset.snapshot_id == SourceProfileTableObservation.snapshot_id,
                    CatalogAsset.id == SourceProfileTableObservation.asset_id,
                ),
            )
            .join(
                SourceTableScope,
                and_(
                    SourceTableScope.source_id == CatalogAsset.source_id,
                    SourceTableScope.schema_name == CatalogAsset.schema_name,
                    SourceTableScope.table_name == CatalogAsset.table_name,
                ),
            )
            .where(
                SourceProfileTableObservation.source_id == source_id,
                SourceProfileTableObservation.run_id == run.id,
            )
        )
        total = (await session.execute(select(func.count()).select_from(statement.subquery()))).scalar_one()
        pairs = (
            await session.execute(
                statement.order_by(
                    CatalogAsset.schema_name,
                    CatalogAsset.table_name,
                    SourceProfileTableObservation.id,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        tables = tuple(
            ProfileTableObservationView(observation=observation, asset=asset)
            for observation, asset in pairs
        )
        observation_ids = [item.observation.id for item in tables]
        columns: tuple[ProfileColumnObservationView, ...] = ()
        if observation_ids:
            column_rows = (
                await session.execute(
                    select(
                        SourceProfileColumnObservation,
                        SourceProfileTableObservation,
                        CatalogAsset,
                        CatalogColumn,
                        SourceProfileRunColumn,
                    )
                    .join(
                        SourceProfileTableObservation,
                        and_(
                            SourceProfileTableObservation.source_id
                            == SourceProfileColumnObservation.source_id,
                            SourceProfileTableObservation.id
                            == SourceProfileColumnObservation.table_observation_id,
                        ),
                    )
                    .join(
                        CatalogAsset,
                        and_(
                            CatalogAsset.source_id == SourceProfileColumnObservation.source_id,
                            CatalogAsset.snapshot_id == SourceProfileColumnObservation.snapshot_id,
                            CatalogAsset.id == SourceProfileColumnObservation.asset_id,
                        ),
                    )
                    .join(
                        CatalogColumn,
                        and_(
                            CatalogColumn.source_id == SourceProfileColumnObservation.source_id,
                            CatalogColumn.snapshot_id == SourceProfileColumnObservation.snapshot_id,
                            CatalogColumn.asset_id == SourceProfileColumnObservation.asset_id,
                            CatalogColumn.id == SourceProfileColumnObservation.column_id,
                        ),
                    )
                    .join(
                        SourceProfileRunColumn,
                        and_(
                            SourceProfileRunColumn.source_id == SourceProfileColumnObservation.source_id,
                            SourceProfileRunColumn.run_id == SourceProfileColumnObservation.run_id,
                            SourceProfileRunColumn.snapshot_id == SourceProfileColumnObservation.snapshot_id,
                            SourceProfileRunColumn.asset_id == SourceProfileColumnObservation.asset_id,
                            SourceProfileRunColumn.column_id == SourceProfileColumnObservation.column_id,
                        ),
                    )
                    .where(
                        SourceProfileColumnObservation.source_id == source_id,
                        SourceProfileColumnObservation.run_id == run.id,
                        SourceProfileColumnObservation.table_observation_id.in_(observation_ids),
                    )
                    .order_by(
                        CatalogAsset.schema_name,
                        CatalogAsset.table_name,
                        CatalogColumn.ordinal_position,
                        CatalogColumn.id,
                    )
                )
            ).all()
            columns = tuple(
                ProfileColumnObservationView(
                    observation=column_observation,
                    table_observation=table_observation,
                    asset=asset,
                    column=column,
                    scalar_kind=selection.scalar_kind,
                )
                for column_observation, table_observation, asset, column, selection in column_rows
            )
        return ProfileObservationPage(
            source_id,
            run,
            page,
            page_size,
            total,
            tables,
            columns,
        )
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


async def list_source_profile_findings_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    page: int,
    page_size: int,
) -> ProfileFindingPage:
    """Read deterministic findings only for assets still within source scope."""

    try:
        run = await get_source_profile_run_for_owner(
            session,
            source_id=source_id,
            run_id=run_id,
            owner_user_id=owner_user_id,
        )
        statement = (
            select(
                SourceProfileFinding,
                SourceProfileTableObservation,
                SourceProfileColumnObservation,
                CatalogAsset,
                CatalogColumn,
            )
            .join(
                SourceProfileTableObservation,
                and_(
                    SourceProfileTableObservation.source_id == SourceProfileFinding.source_id,
                    SourceProfileTableObservation.id == SourceProfileFinding.table_observation_id,
                ),
            )
            .join(
                SourceProfileColumnObservation,
                and_(
                    SourceProfileColumnObservation.source_id == SourceProfileFinding.source_id,
                    SourceProfileColumnObservation.id == SourceProfileFinding.column_observation_id,
                ),
            )
            .join(
                CatalogAsset,
                and_(
                    CatalogAsset.source_id == SourceProfileColumnObservation.source_id,
                    CatalogAsset.snapshot_id == SourceProfileColumnObservation.snapshot_id,
                    CatalogAsset.id == SourceProfileColumnObservation.asset_id,
                ),
            )
            .join(
                CatalogColumn,
                and_(
                    CatalogColumn.source_id == SourceProfileColumnObservation.source_id,
                    CatalogColumn.snapshot_id == SourceProfileColumnObservation.snapshot_id,
                    CatalogColumn.asset_id == SourceProfileColumnObservation.asset_id,
                    CatalogColumn.id == SourceProfileColumnObservation.column_id,
                ),
            )
            .join(
                SourceTableScope,
                and_(
                    SourceTableScope.source_id == CatalogAsset.source_id,
                    SourceTableScope.schema_name == CatalogAsset.schema_name,
                    SourceTableScope.table_name == CatalogAsset.table_name,
                ),
            )
            .where(
                SourceProfileFinding.source_id == source_id,
                SourceProfileFinding.run_id == run.id,
            )
        )
        total = (await session.execute(select(func.count()).select_from(statement.subquery()))).scalar_one()
        rows = (
            await session.execute(
                statement.order_by(SourceProfileFinding.generated_at.desc(), SourceProfileFinding.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return ProfileFindingPage(
            source_id,
            run,
            page,
            page_size,
            total,
            tuple(
                ProfileFindingView(
                    finding=finding,
                    table_observation=table_observation,
                    column_observation=column_observation,
                    asset=asset,
                    column=column,
                )
                for finding, table_observation, column_observation, asset, column in rows
            ),
        )
    except SQLAlchemyError:
        await _rollback(session)
        raise SourceProfilePersistenceError() from None


__all__ = [
    "ProfileAssetExecutionContext",
    "ProfileColumnAggregate",
    "ProfileColumnExecutionContext",
    "ProfileExecutionContext",
    "ProfileFindingPage",
    "ProfileFindingView",
    "ProfileObservationPage",
    "ProfileSelection",
    "ProfileTableAggregate",
    "ProfileRunPage",
    "SourceProfileCatalogUnavailableError",
    "SourceProfileInputError",
    "SourceProfileLease",
    "SourceProfileLeaseLostError",
    "SourceProfilePersistenceError",
    "SourceProfileRunStateError",
    "SourceProfileSupersededError",
    "SourceProfilingDisabledError",
    "cancel_source_profile_run_for_owner",
    "claim_next_source_profile_run",
    "configure_source_profiling_for_owner",
    "finalize_source_profile_worker_failure",
    "get_source_profile_run_for_owner",
    "list_source_profile_findings_for_owner",
    "list_source_profile_observations_for_owner",
    "list_source_profile_runs_for_owner",
    "load_source_profile_execution_context",
    "publish_source_profile_observations",
    "renew_source_profile_lease",
    "request_source_profile_run_for_owner",
]
