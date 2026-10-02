"""Internal persistence boundary for immutable source catalog snapshots.

Stage 3A intentionally has no route, worker, live discovery, or client-query
logic. A later worker supplies normalized structural input to this module only
after it has independently read the client database through the connector.

Run lifecycle: ``QUEUED`` may become ``RUNNING``, ``FAILED``, or ``CANCELLED``;
``RUNNING`` may become ``SUCCEEDED``, ``FAILED``, or ``CANCELLED``. Terminal
states never transition. A successful run is linked to its one immutable
published snapshot by ``CatalogSnapshot.sync_run_id`` (unique per run), and
only the source's separate current-head pointer advances on publication.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

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
    CatalogSyncRunStatus,
    SourceCatalogHead,
)
from app.db.models.source import DataSource, SourceTableScope
from app.exceptions.custom_exceptions import AppError, NotFoundError


class CatalogServiceError(AppError):
    pass


class CatalogInputError(CatalogServiceError):
    status_code = 422
    code = "invalid_catalog_structure"

    def __init__(self, message: str = "catalog structure is invalid"):
        super().__init__(message)


class CatalogRunStateError(CatalogServiceError):
    status_code = 409
    code = "catalog_run_not_publishable"

    def __init__(self):
        super().__init__("catalog synchronization run is not eligible for this operation")


class CatalogScopeChangedError(CatalogServiceError):
    status_code = 409
    code = "catalog_scope_changed"

    def __init__(self):
        super().__init__("source scope changed before catalog publication")


class CatalogRunSupersededError(CatalogServiceError):
    status_code = 409
    code = "catalog_run_superseded"

    def __init__(self):
        super().__init__("a newer catalog synchronization request superseded this run")


class CatalogPersistenceError(CatalogServiceError):
    status_code = 503
    code = "catalog_persistence_failed"

    def __init__(self):
        super().__init__("could not complete the catalog persistence request")


class CatalogRunLeaseLostError(CatalogServiceError):
    status_code = 409
    code = "catalog_run_lease_lost"

    def __init__(self):
        super().__init__("catalog synchronization lease is no longer valid")


@dataclass(frozen=True)
class CatalogTableIdentity:
    """A structural table identity; never concatenate these parts for storage."""

    schema_name: str
    table_name: str


@dataclass(frozen=True)
class CatalogColumnInput:
    column_name: str
    ordinal_position: int
    is_nullable: bool
    formatted_native_type: str
    native_type_schema: str | None = None
    native_type_name: str | None = None
    native_type_oid: int | None = None
    native_type_modifier: int | None = None
    column_comment: str | None = None


@dataclass(frozen=True)
class CatalogKeyInput:
    constraint_name: str
    key_kind: CatalogKeyKind
    column_names: tuple[str, ...]


@dataclass(frozen=True)
class CatalogForeignKeyColumnInput:
    ordinal_position: int
    referencing_column_name: str
    referenced_column_name: str


@dataclass(frozen=True)
class CatalogForeignKeyInput:
    constraint_name: str
    target_status: CatalogForeignKeyTargetStatus
    referenced_table: CatalogTableIdentity | None = None
    column_mappings: tuple[CatalogForeignKeyColumnInput, ...] = ()


@dataclass(frozen=True)
class CatalogAssetInput:
    identity: CatalogTableIdentity
    columns: tuple[CatalogColumnInput, ...]
    declared_keys: tuple[CatalogKeyInput, ...] = ()
    foreign_keys: tuple[CatalogForeignKeyInput, ...] = ()
    table_comment: str | None = None


@dataclass(frozen=True)
class CatalogSnapshotInput:
    """Complete normalized structural metadata for one source scope."""

    assets: tuple[CatalogAssetInput, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CatalogSyncLease:
    """Worker-only durable claim. The opaque token fences stale workers."""

    source_id: uuid.UUID
    run_id: uuid.UUID
    lease_token: uuid.UUID
    attempt_count: int


@dataclass(frozen=True)
class CatalogAvailability:
    source_id: uuid.UUID
    latest_run: CatalogSyncRun | None
    current_snapshot: CatalogSnapshot | None


_SAFE_FAILURE_CODE = re.compile(r"^[a-z0-9_]{1,64}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _identity_key(identity: CatalogTableIdentity) -> tuple[str, str]:
    if not isinstance(identity, CatalogTableIdentity):
        raise CatalogInputError()
    return (
        _validate_identifier(identity.schema_name, "schema name"),
        _validate_identifier(identity.table_name, "table name"),
    )


def _validate_identifier(value: str, label: str) -> str:
    # Quoted PostgreSQL identifiers may intentionally contain spaces, dots,
    # colons, or quotes. Preserve exact text rather than stripping it.
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > 63:
        raise CatalogInputError(f"{label} is invalid")
    return value


def _validate_comment(value: str | None, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise CatalogInputError(f"{label} is invalid")
    return value


def canonical_scope_fingerprint(scopes: Iterable[CatalogTableIdentity]) -> str:
    """Return SHA-256 over sorted structured scope tuples.

    This deliberately hashes JSON arrays such that ``("a.b", "c")`` and
    ``("a", "b.c")`` cannot collide through a display separator.
    """

    pairs = [_identity_key(scope) for scope in scopes]
    if len(set(pairs)) != len(pairs):
        raise CatalogInputError("catalog scope contains duplicate table identities")
    canonical = json.dumps(
        [[schema_name, table_name] for schema_name, table_name in sorted(pairs)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_safe_failure_code(failure_code: str) -> str:
    if not isinstance(failure_code, str) or not _SAFE_FAILURE_CODE.fullmatch(failure_code):
        raise CatalogInputError("catalog failure code is invalid")
    return failure_code


def _validate_ordered_positions(positions: Iterable[int], label: str) -> None:
    ordered = list(positions)
    if ordered != list(range(1, len(ordered) + 1)):
        raise CatalogInputError(f"{label} ordinals must be contiguous and start at one")


def _validate_snapshot_input(snapshot_input: CatalogSnapshotInput) -> dict[tuple[str, str], CatalogAssetInput]:
    if not isinstance(snapshot_input, CatalogSnapshotInput) or not snapshot_input.assets:
        raise CatalogInputError("catalog snapshot must contain at least one asset")

    assets: dict[tuple[str, str], CatalogAssetInput] = {}
    for asset in snapshot_input.assets:
        if not isinstance(asset, CatalogAssetInput):
            raise CatalogInputError()
        identity = _identity_key(asset.identity)
        if identity in assets:
            raise CatalogInputError("catalog snapshot contains duplicate table identities")
        _validate_comment(asset.table_comment, "table comment")
        _validate_asset_columns(asset)
        _validate_asset_keys(asset)
        assets[identity] = asset

    for asset in assets.values():
        _validate_asset_foreign_keys(asset, assets)
    return assets


def _validate_asset_columns(asset: CatalogAssetInput) -> None:
    if not asset.columns:
        raise CatalogInputError("catalog asset must contain at least one column")
    names: list[str] = []
    positions: list[int] = []
    for column in asset.columns:
        if not isinstance(column, CatalogColumnInput):
            raise CatalogInputError()
        names.append(_validate_identifier(column.column_name, "column name"))
        if not isinstance(column.ordinal_position, int) or isinstance(column.ordinal_position, bool):
            raise CatalogInputError("column ordinal is invalid")
        positions.append(column.ordinal_position)
        if not isinstance(column.is_nullable, bool):
            raise CatalogInputError("column nullability is invalid")
        if not isinstance(column.formatted_native_type, str) or not column.formatted_native_type:
            raise CatalogInputError("native column type is invalid")
        for value, label in (
            (column.native_type_schema, "native type schema"),
            (column.native_type_name, "native type name"),
        ):
            if value is not None:
                _validate_identifier(value, label)
        for value, label in (
            (column.native_type_oid, "native type OID"),
            (column.native_type_modifier, "native type modifier"),
        ):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise CatalogInputError(f"{label} is invalid")
        _validate_comment(column.column_comment, "column comment")
    if len(set(names)) != len(names):
        raise CatalogInputError("catalog asset contains duplicate column names")
    _validate_ordered_positions(positions, "column")


def _validate_asset_keys(asset: CatalogAssetInput) -> None:
    column_names = {column.column_name for column in asset.columns}
    constraints: set[str] = set()
    primary_key_count = 0
    for key in asset.declared_keys:
        if not isinstance(key, CatalogKeyInput):
            raise CatalogInputError()
        constraint_name = _validate_identifier(key.constraint_name, "key constraint name")
        if constraint_name in constraints:
            raise CatalogInputError("catalog asset contains duplicate key constraint names")
        constraints.add(constraint_name)
        if not isinstance(key.key_kind, CatalogKeyKind):
            raise CatalogInputError("key kind is invalid")
        if not key.column_names:
            raise CatalogInputError("declared key must contain at least one column")
        names = [_validate_identifier(name, "key column name") for name in key.column_names]
        if len(set(names)) != len(names) or not set(names).issubset(column_names):
            raise CatalogInputError("declared key references an unknown or duplicate column")
        if key.key_kind is CatalogKeyKind.PRIMARY_KEY:
            primary_key_count += 1
    if primary_key_count > 1:
        raise CatalogInputError("catalog asset contains more than one primary key")


def _validate_asset_foreign_keys(
    asset: CatalogAssetInput,
    assets: dict[tuple[str, str], CatalogAssetInput],
) -> None:
    local_columns = {column.column_name for column in asset.columns}
    constraints: set[str] = set()
    for foreign_key in asset.foreign_keys:
        if not isinstance(foreign_key, CatalogForeignKeyInput):
            raise CatalogInputError()
        constraint_name = _validate_identifier(foreign_key.constraint_name, "foreign key constraint name")
        if constraint_name in constraints:
            raise CatalogInputError("catalog asset contains duplicate foreign key constraint names")
        constraints.add(constraint_name)
        if not isinstance(foreign_key.target_status, CatalogForeignKeyTargetStatus):
            raise CatalogInputError("foreign key target status is invalid")

        if foreign_key.target_status is CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE:
            if foreign_key.referenced_table is not None or foreign_key.column_mappings:
                raise CatalogInputError("out-of-scope foreign key target must omit target details")
            continue

        if foreign_key.referenced_table is None:
            raise CatalogInputError("in-scope foreign key target is missing")
        target = assets.get(_identity_key(foreign_key.referenced_table))
        if target is None:
            raise CatalogInputError("foreign key target is outside the catalog snapshot")
        if not foreign_key.column_mappings:
            raise CatalogInputError("in-scope foreign key must contain column mappings")
        target_columns = {column.column_name for column in target.columns}
        positions: list[int] = []
        source_names: list[str] = []
        target_names: list[str] = []
        for mapping in foreign_key.column_mappings:
            if not isinstance(mapping, CatalogForeignKeyColumnInput):
                raise CatalogInputError()
            if not isinstance(mapping.ordinal_position, int) or isinstance(mapping.ordinal_position, bool):
                raise CatalogInputError("foreign key ordinal is invalid")
            positions.append(mapping.ordinal_position)
            source_names.append(_validate_identifier(mapping.referencing_column_name, "referencing column name"))
            target_names.append(_validate_identifier(mapping.referenced_column_name, "referenced column name"))
        _validate_ordered_positions(positions, "foreign key")
        if (
            len(set(source_names)) != len(source_names)
            or len(set(target_names)) != len(target_names)
            or not set(source_names).issubset(local_columns)
            or not set(target_names).issubset(target_columns)
        ):
            raise CatalogInputError("foreign key references an unknown or duplicate column")


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass


async def _commit_or_raise(session: AsyncSession) -> None:
    try:
        await session.commit()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def _locked_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
) -> DataSource | None:
    statement = select(DataSource).where(DataSource.id == source_id)
    return (await session.execute(statement.with_for_update())).scalar_one_or_none()


async def _locked_owned_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
) -> DataSource | None:
    """Lock a source only when a concrete trusted owner identity is present.

    Internal worker operations deliberately use ``_locked_source`` after their
    queue contract has identified a run. Owner-facing operations must never
    let ``None`` mean "omit the ownership predicate".
    """

    if not isinstance(owner_user_id, uuid.UUID):
        return None
    statement = select(DataSource).where(
        DataSource.id == source_id,
        DataSource.owner_user_id == owner_user_id,
    )
    return (await session.execute(statement.with_for_update())).scalar_one_or_none()


async def _locked_run(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
) -> CatalogSyncRun | None:
    return (
        await session.execute(
            select(CatalogSyncRun)
            .where(CatalogSyncRun.id == run_id, CatalogSyncRun.source_id == source_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


def _clear_lease(run: CatalogSyncRun) -> None:
    run.lease_owner = None
    run.lease_token = None
    run.lease_expires_at = None


def _lease_expired(lease_expires_at: datetime, now: datetime) -> bool:
    """Compare application timestamps across PostgreSQL and SQLite tests."""

    if lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
    return lease_expires_at <= now


def _active_or_recoverable_run_query(source_id: uuid.UUID, now: datetime):
    return (
        select(CatalogSyncRun)
        .where(
            CatalogSyncRun.source_id == source_id,
            or_(
                CatalogSyncRun.status == CatalogSyncRunStatus.QUEUED,
                and_(
                    CatalogSyncRun.status == CatalogSyncRunStatus.RUNNING,
                    or_(
                        # Stage 3A's internal transition helper pre-dates
                        # leases. Treat such a non-leased RUNNING row as
                        # abandoned so a Stage 3B worker cannot strand it.
                        CatalogSyncRun.lease_expires_at.is_(None),
                        CatalogSyncRun.lease_expires_at <= now,
                    ),
                ),
            ),
        )
        .order_by(CatalogSyncRun.request_sequence.desc())
    )


def _active_run_query(source_id: uuid.UUID):
    return (
        select(CatalogSyncRun)
        .where(
            CatalogSyncRun.source_id == source_id,
            CatalogSyncRun.status.in_(
                (CatalogSyncRunStatus.QUEUED, CatalogSyncRunStatus.RUNNING)
            ),
        )
        .order_by(CatalogSyncRun.request_sequence.desc())
    )


async def _current_scope(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    lock: bool = False,
) -> tuple[CatalogTableIdentity, ...]:
    statement = (
        select(SourceTableScope.schema_name, SourceTableScope.table_name)
        .where(SourceTableScope.source_id == source_id)
        .order_by(SourceTableScope.schema_name, SourceTableScope.table_name)
    )
    if lock:
        # Callers already hold the source row. Lock current scope rows too so
        # a deletion cannot race the capture/recheck and publication window.
        # Future scope mutation code must acquire the source lock first, then
        # any scope-row lock, in this same order.
        statement = statement.with_for_update()
    rows = await session.execute(statement)
    return tuple(CatalogTableIdentity(schema_name=row[0], table_name=row[1]) for row in rows.all())


async def _owned_source_or_raise(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    lock: bool = False,
) -> DataSource:
    if not isinstance(owner_user_id, uuid.UUID):
        await _rollback_quietly(session)
        raise NotFoundError("source not found")
    statement = select(DataSource).where(
        DataSource.id == source_id, DataSource.owner_user_id == owner_user_id
    )
    if lock:
        statement = statement.with_for_update()
    source = (await session.execute(statement)).scalar_one_or_none()
    if source is None:
        await _rollback_quietly(session)
        raise NotFoundError("source not found")
    return source


async def request_catalog_sync_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogSyncRun:
    """Capture the current explicit scope in a queued, owner-scoped run."""

    try:
        source = await _locked_owned_source(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        if source is None:
            await _rollback_quietly(session)
            raise NotFoundError("source not found")
        scope = await _current_scope(session, source.id, lock=True)
        if not scope:
            await _rollback_quietly(session)
            raise CatalogInputError("source has no selected table scope")
        scope_fingerprint = canonical_scope_fingerprint(scope)
        active = (
            await session.execute(
                _active_run_query(source.id).with_for_update()
            )
        ).scalars().first()
        if active is not None:
            if (
                active.scope_fingerprint == scope_fingerprint
                and active.scope_count == len(scope)
                and active.request_sequence == source.catalog_sync_sequence
            ):
                # Repeated manual refresh requests coalesce to the one durable
                # queued/leased run rather than growing an unbounded queue.
                await _commit_or_raise(session)
                return active
            active.status = CatalogSyncRunStatus.FAILED
            active.completed_at = _now()
            active.failure_code = "scope_changed"
            active.next_attempt_at = None
            _clear_lease(active)
        source.catalog_sync_sequence += 1
        run = CatalogSyncRun(
            id=uuid.uuid4(),
            source_id=source.id,
            requesting_user_id=owner_user_id,
            request_sequence=source.catalog_sync_sequence,
            scope_fingerprint=scope_fingerprint,
            scope_count=len(scope),
            status=CatalogSyncRunStatus.QUEUED,
            requested_at=_now(),
        )
        session.add(run)
        await _commit_or_raise(session)
        return run
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def claim_next_catalog_sync_run(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: float,
    max_attempts: int,
) -> CatalogSyncLease | None:
    """Atomically claim one current queued or abandoned run for a worker.

    PostgreSQL locks the source row with ``SKIP LOCKED`` before the run row,
    matching all other source/run mutations. That is the per-source fencing
    boundary across independent worker processes; no process-local queue is
    involved.
    """

    if (
        not isinstance(worker_id, str)
        or not worker_id
        or "\x00" in worker_id
        or len(worker_id.encode("utf-8")) > 128
        or lease_seconds <= 0
        or max_attempts < 1
    ):
        raise CatalogInputError("catalog worker claim is invalid")
    now = _now()
    eligible = or_(
        and_(
            CatalogSyncRun.status == CatalogSyncRunStatus.QUEUED,
            or_(
                CatalogSyncRun.next_attempt_at.is_(None),
                CatalogSyncRun.next_attempt_at <= now,
            ),
        ),
        and_(
            CatalogSyncRun.status == CatalogSyncRunStatus.RUNNING,
            or_(
                CatalogSyncRun.lease_expires_at.is_(None),
                CatalogSyncRun.lease_expires_at <= now,
            ),
        ),
    )
    try:
        candidate_source_id = (
            await session.execute(
                select(DataSource.id)
                .join(CatalogSyncRun, CatalogSyncRun.source_id == DataSource.id)
                .where(
                    eligible,
                    CatalogSyncRun.request_sequence == DataSource.catalog_sync_sequence,
                )
                .order_by(CatalogSyncRun.requested_at, CatalogSyncRun.id)
                .with_for_update(of=DataSource, skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if candidate_source_id is None:
            await _commit_or_raise(session)
            return None
        source = await _locked_source(session, source_id=candidate_source_id)
        if source is None:
            await _rollback_quietly(session)
            return None
        run = (
            await session.execute(
                _active_or_recoverable_run_query(source.id, now).where(
                    CatalogSyncRun.request_sequence == source.catalog_sync_sequence
                ).with_for_update()
            )
        ).scalars().first()
        if run is None:
            await _commit_or_raise(session)
            return None
        if run.attempt_count >= max_attempts:
            run.status = CatalogSyncRunStatus.FAILED
            run.completed_at = now
            run.failure_code = "retry_exhausted"
            _clear_lease(run)
            await _commit_or_raise(session)
            return None
        token = uuid.uuid4()
        run.status = CatalogSyncRunStatus.RUNNING
        run.started_at = run.started_at or now
        run.completed_at = None
        run.failure_code = None
        run.attempt_count += 1
        run.lease_owner = worker_id
        run.lease_token = token
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        run.next_attempt_at = None
        await _commit_or_raise(session)
        return CatalogSyncLease(
            source_id=source.id,
            run_id=run.id,
            lease_token=token,
            attempt_count=run.attempt_count,
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def renew_catalog_sync_lease(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    lease_token: uuid.UUID,
    lease_seconds: float,
) -> bool:
    """Extend a lease only if the caller remains the current fenced worker."""

    if lease_seconds <= 0:
        raise CatalogInputError("catalog lease duration is invalid")
    now = _now()
    try:
        source = await _locked_source(session, source_id=source_id)
        run = await _locked_run(session, source_id=source_id, run_id=run_id) if source else None
        if (
            source is None
            or run is None
            or run.status is not CatalogSyncRunStatus.RUNNING
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or _lease_expired(run.lease_expires_at, now)
            or run.cancel_requested_at is not None
        ):
            await _rollback_quietly(session)
            return False
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await _commit_or_raise(session)
        return True
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def finalize_catalog_sync_worker_failure(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    lease_token: uuid.UUID,
    failure_code: str,
    retry_delay_seconds: float | None,
    max_attempts: int,
) -> bool:
    """Fence a worker's retry/terminal result at the durable run row."""

    safe_failure_code = _validate_safe_failure_code(failure_code)
    if max_attempts < 1 or (retry_delay_seconds is not None and retry_delay_seconds < 0):
        raise CatalogInputError("catalog retry configuration is invalid")
    now = _now()
    try:
        source = await _locked_source(session, source_id=source_id)
        run = await _locked_run(session, source_id=source_id, run_id=run_id) if source else None
        if (
            source is None
            or run is None
            or run.status is not CatalogSyncRunStatus.RUNNING
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or _lease_expired(run.lease_expires_at, now)
            or run.cancel_requested_at is not None
        ):
            await _rollback_quietly(session)
            return False
        _clear_lease(run)
        run.failure_code = safe_failure_code
        if retry_delay_seconds is not None and run.attempt_count < max_attempts:
            run.status = CatalogSyncRunStatus.QUEUED
            run.next_attempt_at = now + timedelta(seconds=retry_delay_seconds)
            run.completed_at = None
        else:
            run.status = CatalogSyncRunStatus.FAILED
            run.completed_at = now
            run.next_attempt_at = None
        await _commit_or_raise(session)
        return True
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def mark_catalog_sync_running(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
) -> CatalogSyncRun:
    """Transition a queued run to running before a future worker does I/O."""

    try:
        source = await _locked_source(session, source_id=source_id)
        run = await _locked_run(session, source_id=source_id, run_id=run_id) if source else None
        if source is None or run is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog synchronization run not found")
        if run.status is not CatalogSyncRunStatus.QUEUED:
            await _rollback_quietly(session)
            raise CatalogRunStateError()
        if run.request_sequence != source.catalog_sync_sequence:
            run.status = CatalogSyncRunStatus.CANCELLED
            run.completed_at = _now()
            run.failure_code = "superseded"
            run.next_attempt_at = None
            _clear_lease(run)
            await _commit_or_raise(session)
            raise CatalogRunSupersededError()
        if (
            canonical_scope_fingerprint(await _current_scope(session, source.id, lock=True))
            != run.scope_fingerprint
        ):
            run.status = CatalogSyncRunStatus.FAILED
            run.completed_at = _now()
            run.failure_code = "scope_changed"
            run.next_attempt_at = None
            _clear_lease(run)
            await _commit_or_raise(session)
            raise CatalogScopeChangedError()
        run.status = CatalogSyncRunStatus.RUNNING
        run.started_at = _now()
        run.failure_code = None
        await _commit_or_raise(session)
        return run
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def fail_catalog_sync_run(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    failure_code: str,
) -> CatalogSyncRun:
    """Record a safe worker failure without touching a current snapshot."""

    safe_failure_code = _validate_safe_failure_code(failure_code)
    try:
        source = await _locked_source(session, source_id=source_id)
        run = await _locked_run(session, source_id=source_id, run_id=run_id) if source else None
        if source is None or run is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog synchronization run not found")
        if run.status not in {CatalogSyncRunStatus.QUEUED, CatalogSyncRunStatus.RUNNING}:
            await _rollback_quietly(session)
            raise CatalogRunStateError()
        run.status = CatalogSyncRunStatus.FAILED
        run.completed_at = _now()
        run.failure_code = safe_failure_code
        run.next_attempt_at = None
        _clear_lease(run)
        await _commit_or_raise(session)
        return run
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def cancel_catalog_sync_run_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogSyncRun:
    """Cancel a queued/running run through an owner-scoped path."""

    try:
        source = await _locked_owned_source(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        run = await _locked_run(session, source_id=source_id, run_id=run_id) if source else None
        if source is None or run is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog synchronization run not found")
        if run.status not in {CatalogSyncRunStatus.QUEUED, CatalogSyncRunStatus.RUNNING}:
            await _rollback_quietly(session)
            raise CatalogRunStateError()
        run.cancel_requested_at = _now()
        run.status = CatalogSyncRunStatus.CANCELLED
        run.completed_at = _now()
        run.failure_code = None
        run.next_attempt_at = None
        _clear_lease(run)
        await _commit_or_raise(session)
        return run
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def publish_catalog_snapshot(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    snapshot_input: CatalogSnapshotInput,
    lease_token: uuid.UUID | None = None,
) -> CatalogSnapshot:
    """Atomically publish a complete structural snapshot for a running run.

    The caller must have already performed remote discovery. This function only
    validates and persists normalized metadata; it never decrypts a source,
    contacts a client database, or accepts a user-supplied owner identity.
    """

    assets_by_identity = _validate_snapshot_input(snapshot_input)
    try:
        # Lock source before run everywhere in this module to serialize source
        # sequence allocation, cancellation, and head movement across workers.
        source = await _locked_source(session, source_id=source_id)
        run = await _locked_run(session, source_id=source_id, run_id=run_id) if source else None
        if source is None or run is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog synchronization run not found")
        if run.status is not CatalogSyncRunStatus.RUNNING:
            await _rollback_quietly(session)
            raise CatalogRunStateError()
        if run.lease_token is not None or lease_token is not None:
            if (
                lease_token is None
                or run.lease_token != lease_token
                or run.lease_expires_at is None
                or _lease_expired(run.lease_expires_at, _now())
                or run.cancel_requested_at is not None
            ):
                await _rollback_quietly(session)
                raise CatalogRunLeaseLostError()
        if run.request_sequence != source.catalog_sync_sequence:
            run.status = CatalogSyncRunStatus.CANCELLED
            run.completed_at = _now()
            run.failure_code = "superseded"
            run.next_attempt_at = None
            _clear_lease(run)
            await _commit_or_raise(session)
            raise CatalogRunSupersededError()

        current_scope = await _current_scope(session, source.id, lock=True)
        if (
            len(current_scope) != run.scope_count
            or canonical_scope_fingerprint(current_scope) != run.scope_fingerprint
        ):
            run.status = CatalogSyncRunStatus.FAILED
            run.completed_at = _now()
            run.failure_code = "scope_changed"
            run.next_attempt_at = None
            _clear_lease(run)
            await _commit_or_raise(session)
            raise CatalogScopeChangedError()

        current_scope_identities = {_identity_key(identity) for identity in current_scope}
        if set(assets_by_identity) != current_scope_identities:
            await _rollback_quietly(session)
            raise CatalogInputError("catalog assets must exactly match the captured selected scope")

        snapshot = CatalogSnapshot(
            id=uuid.uuid4(),
            source_id=source.id,
            sync_run_id=run.id,
            scope_fingerprint=run.scope_fingerprint,
            published_at=_now(),
        )
        session.add(snapshot)
        # The immutable header must exist before its composite-FK children.
        # This remains inside the transaction, so it is never observable as a
        # partial current snapshot.
        await session.flush()
        _persist_snapshot_children(
            session,
            source_id=source.id,
            snapshot_id=snapshot.id,
            assets_by_identity=assets_by_identity,
        )

        # Flush every immutable structural row before changing the current
        # pointer. A constraint error rolls back this whole transaction.
        await session.flush()
        head = await session.get(SourceCatalogHead, source.id)
        if head is None:
            session.add(SourceCatalogHead(source_id=source.id, snapshot_id=snapshot.id))
        else:
            head.snapshot_id = snapshot.id
        run.status = CatalogSyncRunStatus.SUCCEEDED
        run.completed_at = _now()
        run.failure_code = None
        run.next_attempt_at = None
        _clear_lease(run)
        await _commit_or_raise(session)
        return snapshot
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


def _persist_snapshot_children(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    assets_by_identity: dict[tuple[str, str], CatalogAssetInput],
) -> None:
    asset_ids = {identity: uuid.uuid4() for identity in assets_by_identity}
    column_ids: dict[tuple[tuple[str, str], str], uuid.UUID] = {}

    for identity, asset in assets_by_identity.items():
        schema_name, table_name = identity
        session.add(
            CatalogAsset(
                id=asset_ids[identity],
                source_id=source_id,
                snapshot_id=snapshot_id,
                schema_name=schema_name,
                table_name=table_name,
                table_comment=asset.table_comment,
            )
        )
        for column in asset.columns:
            column_id = uuid.uuid4()
            column_ids[(identity, column.column_name)] = column_id
            session.add(
                CatalogColumn(
                    id=column_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    asset_id=asset_ids[identity],
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
            )

    for identity, asset in assets_by_identity.items():
        for key in asset.declared_keys:
            key_id = uuid.uuid4()
            session.add(
                CatalogDeclaredKey(
                    id=key_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    asset_id=asset_ids[identity],
                    constraint_name=key.constraint_name,
                    key_kind=key.key_kind,
                )
            )
            for ordinal_position, column_name in enumerate(key.column_names, start=1):
                session.add(
                    CatalogDeclaredKeyColumn(
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        asset_id=asset_ids[identity],
                        key_id=key_id,
                        column_id=column_ids[(identity, column_name)],
                        ordinal_position=ordinal_position,
                    )
                )

    for identity, asset in assets_by_identity.items():
        for foreign_key in asset.foreign_keys:
            foreign_key_id = uuid.uuid4()
            target_identity = (
                _identity_key(foreign_key.referenced_table)
                if foreign_key.referenced_table is not None
                else None
            )
            target_asset_id = asset_ids[target_identity] if target_identity is not None else None
            session.add(
                CatalogForeignKey(
                    id=foreign_key_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    referencing_asset_id=asset_ids[identity],
                    constraint_name=foreign_key.constraint_name,
                    target_status=foreign_key.target_status,
                    referenced_asset_id=target_asset_id,
                )
            )
            if target_identity is None:
                continue
            for mapping in foreign_key.column_mappings:
                session.add(
                    CatalogForeignKeyColumn(
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        foreign_key_id=foreign_key_id,
                        ordinal_position=mapping.ordinal_position,
                        referencing_asset_id=asset_ids[identity],
                        referenced_asset_id=asset_ids[target_identity],
                        referencing_column_id=column_ids[(identity, mapping.referencing_column_name)],
                        referenced_column_id=column_ids[
                            (target_identity, mapping.referenced_column_name)
                        ],
                    )
                )


async def get_current_catalog_snapshot_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogSnapshot | None:
    """Return the current snapshot only after proving source ownership."""

    try:
        await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        snapshot = (
            await session.execute(
                select(CatalogSnapshot)
                .join(
                    SourceCatalogHead,
                    (SourceCatalogHead.source_id == CatalogSnapshot.source_id)
                    & (SourceCatalogHead.snapshot_id == CatalogSnapshot.id),
                )
                .where(SourceCatalogHead.source_id == source_id)
            )
        ).scalar_one_or_none()
        return snapshot
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def get_catalog_snapshot_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogSnapshot:
    """Read an immutable historical snapshot through the source owner scope."""

    try:
        await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        snapshot = (
            await session.execute(
                select(CatalogSnapshot).where(
                    CatalogSnapshot.id == snapshot_id,
                    CatalogSnapshot.source_id == source_id,
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog snapshot not found")
        return snapshot
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def get_catalog_sync_run_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogSyncRun:
    """Read one run only through its source's JWT-derived owner boundary."""

    try:
        await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        run = (
            await session.execute(
                select(CatalogSyncRun).where(
                    CatalogSyncRun.id == run_id,
                    CatalogSyncRun.source_id == source_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog synchronization run not found")
        return run
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def get_catalog_availability_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogAvailability:
    """Return safe Stage 3C availability facts without source credentials."""

    try:
        await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        latest_run = (
            await session.execute(
                select(CatalogSyncRun)
                .where(CatalogSyncRun.source_id == source_id)
                .order_by(CatalogSyncRun.request_sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        current_snapshot = (
            await session.execute(
                select(CatalogSnapshot)
                .join(
                    SourceCatalogHead,
                    (SourceCatalogHead.source_id == CatalogSnapshot.source_id)
                    & (SourceCatalogHead.snapshot_id == CatalogSnapshot.id),
                )
                .where(SourceCatalogHead.source_id == source_id)
            )
        ).scalar_one_or_none()
        return CatalogAvailability(
            source_id=source_id,
            latest_run=latest_run,
            current_snapshot=current_snapshot,
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


__all__ = [
    "CatalogAssetInput",
    "CatalogAvailability",
    "CatalogColumnInput",
    "CatalogForeignKeyColumnInput",
    "CatalogForeignKeyInput",
    "CatalogInputError",
    "CatalogKeyInput",
    "CatalogPersistenceError",
    "CatalogRunLeaseLostError",
    "CatalogRunStateError",
    "CatalogRunSupersededError",
    "CatalogScopeChangedError",
    "CatalogSnapshotInput",
    "CatalogSyncLease",
    "CatalogTableIdentity",
    "cancel_catalog_sync_run_for_owner",
    "canonical_scope_fingerprint",
    "claim_next_catalog_sync_run",
    "fail_catalog_sync_run",
    "finalize_catalog_sync_worker_failure",
    "get_catalog_availability_for_owner",
    "get_catalog_sync_run_for_owner",
    "get_catalog_snapshot_for_owner",
    "get_current_catalog_snapshot_for_owner",
    "mark_catalog_sync_running",
    "publish_catalog_snapshot",
    "renew_catalog_sync_lease",
    "request_catalog_sync_for_owner",
]
