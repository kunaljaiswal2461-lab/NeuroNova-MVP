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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
from app.db.models.source import DataSource, SourceTableScope, SourceType
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


class CatalogInspectionInputError(CatalogServiceError):
    """A bounded public catalog-inspection request was malformed."""

    status_code = 422
    code = "invalid_catalog_inspection_request"

    def __init__(self, message: str = "catalog inspection request is invalid"):
        super().__init__(message)


_POSTGRES_QUERY_CONTEXT_MAX_COLUMN_IDS = 256


class CatalogQueryContextError(CatalogServiceError):
    """A source's current catalog cannot safely support query planning yet.

    This deliberately does not distinguish a stale snapshot, a changed scope,
    or a non-published run. Those details are operational state, not a reason
    to let a caller bypass the current-catalog boundary.
    """

    status_code = 409
    code = "catalog_query_context_unavailable"

    def __init__(self):
        super().__init__("the current catalog is not eligible for query planning")


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


@dataclass(frozen=True)
class PostgresQueryCatalogContext:
    """Authorized, current structural inputs for one PostgreSQL query plan.

    This boundary deliberately returns catalog records rather than names from
    an untrusted plan. Callers can compile only the exact source/snapshot/table
    and requested columns that were proven current and selected here. It does
    not establish live client-database permissions; Stage 4B must recheck
    those immediately before execution.
    """

    source: DataSource
    snapshot: CatalogSnapshot
    asset: CatalogAsset
    columns: Mapping[uuid.UUID, CatalogColumn]
    current_scope_fingerprint: str


@dataclass(frozen=True)
class PostgresChatCatalogContext:
    """The current, owner-authorized catalog surface for PostgreSQL chat.

    This is deliberately structural metadata only.  It is not an execution
    capability, does not contain credentials, and is useful only while its
    ``snapshot`` remains the current published head with an unchanged saved
    scope.  The Stage 4B gateway re-checks all of those facts before remote
    I/O and again before it releases result rows.
    """

    source_id: uuid.UUID
    snapshot: CatalogSnapshot
    assets: tuple[CatalogAsset, ...]
    current_scope_fingerprint: str


@dataclass(frozen=True)
class CatalogAssetReference:
    """Structured table identity for a safe public catalog response."""

    asset_id: uuid.UUID
    schema_name: str
    table_name: str


@dataclass(frozen=True)
class CatalogAssetSummary:
    asset_id: uuid.UUID
    schema_name: str
    table_name: str
    table_comment: str | None


@dataclass(frozen=True)
class CatalogColumnMetadata:
    column_name: str
    ordinal_position: int
    is_nullable: bool
    formatted_native_type: str
    native_type_schema: str | None
    native_type_name: str | None
    native_type_oid: int | None
    native_type_modifier: int | None
    column_comment: str | None


@dataclass(frozen=True)
class CatalogDeclaredKeyMetadata:
    constraint_name: str
    key_kind: CatalogKeyKind
    column_names: tuple[str, ...]


@dataclass(frozen=True)
class CatalogForeignKeyColumnMapping:
    ordinal_position: int
    referencing_column_name: str
    referenced_column_name: str


@dataclass(frozen=True)
class CatalogForeignKeyMetadata:
    """A public FK representation with target data redacted when necessary."""

    constraint_name: str
    target_status: CatalogForeignKeyTargetStatus
    referenced_asset: CatalogAssetReference | None
    column_mappings: tuple[CatalogForeignKeyColumnMapping, ...]


@dataclass(frozen=True)
class CatalogAssetDetail:
    source_id: uuid.UUID
    snapshot: CatalogSnapshot
    scope_matches_current_selection: bool
    asset: CatalogAssetSummary
    columns: tuple[CatalogColumnMetadata, ...]
    declared_keys: tuple[CatalogDeclaredKeyMetadata, ...]
    foreign_keys: tuple[CatalogForeignKeyMetadata, ...]


@dataclass(frozen=True)
class CatalogAssetPage:
    source_id: uuid.UUID
    snapshot: CatalogSnapshot
    scope_matches_current_selection: bool
    page: int
    page_size: int
    total: int
    assets: tuple[CatalogAssetSummary, ...]


@dataclass(frozen=True)
class CatalogTableChange:
    schema_name: str
    table_name: str


@dataclass(frozen=True)
class CatalogColumnIdentityChange:
    schema_name: str
    table_name: str
    column_name: str


@dataclass(frozen=True)
class CatalogColumnChange:
    schema_name: str
    table_name: str
    column_name: str
    previous_formatted_native_type: str
    current_formatted_native_type: str
    previous_is_nullable: bool
    current_is_nullable: bool


@dataclass(frozen=True)
class CatalogKeyChange:
    change: str
    schema_name: str
    table_name: str
    constraint_name: str
    key_kind: CatalogKeyKind
    column_names: tuple[str, ...]


@dataclass(frozen=True)
class CatalogForeignKeyChange:
    change: str
    schema_name: str
    table_name: str
    constraint_name: str
    target_status: CatalogForeignKeyTargetStatus
    referenced_asset: CatalogAssetReference | None
    column_mappings: tuple[CatalogForeignKeyColumnMapping, ...]


@dataclass(frozen=True)
class CatalogCommentChange:
    subject: str
    schema_name: str
    table_name: str
    column_name: str | None
    previous_comment: str | None
    current_comment: str | None


@dataclass(frozen=True)
class CatalogStructuralDiff:
    """Deterministic, bounded structural comparison for two snapshots."""

    source_id: uuid.UUID
    snapshot: CatalogSnapshot
    state: str
    previous_snapshot: CatalogSnapshot | None
    truncated: bool
    change_limit: int
    tables_added: tuple[CatalogTableChange, ...] = ()
    tables_removed: tuple[CatalogTableChange, ...] = ()
    columns_added: tuple[CatalogColumnIdentityChange, ...] = ()
    columns_removed: tuple[CatalogColumnIdentityChange, ...] = ()
    columns_changed: tuple[CatalogColumnChange, ...] = ()
    keys_changed: tuple[CatalogKeyChange, ...] = ()
    foreign_keys_changed: tuple[CatalogForeignKeyChange, ...] = ()
    comments_changed: tuple[CatalogCommentChange, ...] = ()


@dataclass(frozen=True)
class _CatalogAssetState:
    asset: CatalogAssetSummary
    columns: tuple[CatalogColumnMetadata, ...]
    declared_keys: tuple[CatalogDeclaredKeyMetadata, ...]
    foreign_keys: tuple[CatalogForeignKeyMetadata, ...]


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


_CATALOG_INSPECTION_MAX_PAGE_SIZE = 100
_CATALOG_INSPECTION_MAX_PAGE = 10_000
_CATALOG_INSPECTION_MAX_SEARCH_BYTES = 128
_CATALOG_DIFF_CHANGE_LIMIT = 500


def _asset_summary(asset: CatalogAsset) -> CatalogAssetSummary:
    return CatalogAssetSummary(
        asset_id=asset.id,
        schema_name=asset.schema_name,
        table_name=asset.table_name,
        table_comment=asset.table_comment,
    )


def _asset_reference(asset: CatalogAsset) -> CatalogAssetReference:
    return CatalogAssetReference(
        asset_id=asset.id,
        schema_name=asset.schema_name,
        table_name=asset.table_name,
    )


def _column_metadata(column: CatalogColumn) -> CatalogColumnMetadata:
    return CatalogColumnMetadata(
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


def _scope_key_from_asset(asset: CatalogAsset) -> tuple[str, str]:
    return (asset.schema_name, asset.table_name)


def _validated_inspection_page(page: int, page_size: int) -> tuple[int, int]:
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
        or page > _CATALOG_INSPECTION_MAX_PAGE
        or not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > _CATALOG_INSPECTION_MAX_PAGE_SIZE
    ):
        raise CatalogInspectionInputError("catalog page parameters are invalid")
    return page, page_size


def _normalized_asset_search(search: str | None) -> str | None:
    if search is None:
        return None
    if not isinstance(search, str) or "\x00" in search:
        raise CatalogInspectionInputError("catalog search is invalid")
    search = search.strip()
    if not search:
        return None
    if len(search.encode("utf-8")) > _CATALOG_INSPECTION_MAX_SEARCH_BYTES:
        raise CatalogInspectionInputError("catalog search is too long")
    return search


def _literal_ilike_pattern(value: str) -> str:
    """Escape wildcard characters so metadata search is literal and bounded."""

    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _catalog_snapshot_for_source_or_raise(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> CatalogSnapshot:
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


async def _current_scope_keys(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
) -> tuple[CatalogTableIdentity, ...]:
    return await _current_scope(session, source_id)


def _scope_matches_snapshot(
    snapshot: CatalogSnapshot,
    current_scope: tuple[CatalogTableIdentity, ...],
) -> bool:
    return snapshot.scope_fingerprint == canonical_scope_fingerprint(current_scope)


def _scope_asset_join():
    return and_(
        SourceTableScope.source_id == CatalogAsset.source_id,
        SourceTableScope.schema_name == CatalogAsset.schema_name,
        SourceTableScope.table_name == CatalogAsset.table_name,
    )


def _query_context_column_ids(
    column_ids: Iterable[uuid.UUID],
) -> tuple[uuid.UUID, ...]:
    """Materialize bounded callers' column references without accepting IDs by name.

    Query-plan shape validation owns its own complexity limits. This service
    still rejects malformed references here so a direct internal caller cannot
    cause an unscoped catalog lookup. Repeated UUIDs describe the same catalog
    record and are intentionally loaded once.
    """

    try:
        requested = tuple(column_ids)
    except TypeError:
        raise NotFoundError("catalog column not found") from None
    if (
        len(requested) > _POSTGRES_QUERY_CONTEXT_MAX_COLUMN_IDS
        or any(not isinstance(column_id, uuid.UUID) for column_id in requested)
    ):
        raise NotFoundError("catalog column not found")
    return tuple(dict.fromkeys(requested))


async def resolve_postgres_query_catalog_context_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    asset_id: uuid.UUID,
    column_ids: Iterable[uuid.UUID],
    owner_user_id: uuid.UUID,
) -> PostgresQueryCatalogContext:
    """Resolve the sole current, selected catalog context for a query plan.

    An immutable historical snapshot is useful for inspection, but it is not
    an execution authority. Query planning therefore accepts only the source
    head's published snapshot and only when its captured scope still exactly
    matches the source's current explicit table selection. No requested name
    or identifier is trusted until it is found through this owner-scoped,
    source/snapshot/asset-constrained lookup.
    """

    requested_column_ids = _query_context_column_ids(column_ids)
    try:
        source = await _owned_source_or_raise(
            session,
            source_id=source_id,
            owner_user_id=owner_user_id,
        )
        if source.source_type is not SourceType.POSTGRESQL:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        # Distinguish a foreign/nonexistent snapshot (a reference failure)
        # from an owned but historical snapshot (a stale planning context).
        snapshot = (
            await session.execute(
                select(CatalogSnapshot).where(
                    CatalogSnapshot.id == snapshot_id,
                    CatalogSnapshot.source_id == source.id,
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog snapshot not found")

        head = (
            await session.execute(
                select(SourceCatalogHead).where(SourceCatalogHead.source_id == source.id)
            )
        ).scalar_one_or_none()
        if head is None or head.snapshot_id != snapshot.id:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        run = (
            await session.execute(
                select(CatalogSyncRun).where(
                    CatalogSyncRun.id == snapshot.sync_run_id,
                    CatalogSyncRun.source_id == source.id,
                )
            )
        ).scalar_one_or_none()
        if run is None or run.status is not CatalogSyncRunStatus.SUCCEEDED:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        current_scope = await _current_scope(session, source.id)
        if not current_scope:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()
        current_scope_fingerprint = canonical_scope_fingerprint(current_scope)
        if snapshot.scope_fingerprint != current_scope_fingerprint:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        asset = (
            await session.execute(
                select(CatalogAsset)
                .join(SourceTableScope, _scope_asset_join())
                .where(
                    CatalogAsset.id == asset_id,
                    CatalogAsset.source_id == source.id,
                    CatalogAsset.snapshot_id == snapshot.id,
                )
            )
        ).scalar_one_or_none()
        if asset is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog asset not found")

        columns: dict[uuid.UUID, CatalogColumn] = {}
        if requested_column_ids:
            resolved_columns = (
                await session.execute(
                    select(CatalogColumn)
                    .where(
                        CatalogColumn.source_id == source.id,
                        CatalogColumn.snapshot_id == snapshot.id,
                        CatalogColumn.asset_id == asset.id,
                        CatalogColumn.id.in_(requested_column_ids),
                    )
                    .order_by(CatalogColumn.ordinal_position, CatalogColumn.id)
                )
            ).scalars().all()
            columns = {column.id: column for column in resolved_columns}
            if len(columns) != len(requested_column_ids):
                await _rollback_quietly(session)
                raise NotFoundError("catalog column not found")

        return PostgresQueryCatalogContext(
            source=source,
            snapshot=snapshot,
            asset=asset,
            columns=MappingProxyType(columns),
            current_scope_fingerprint=current_scope_fingerprint,
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def get_postgres_chat_catalog_context_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> PostgresChatCatalogContext:
    """Return the sole current catalog surface that database chat may plan on.

    Browsing historical catalog snapshots is intentionally supported elsewhere,
    but database chat cannot plan from them.  This helper proves owner access,
    the current head, a successful publication, and an unchanged explicit
    source scope before exposing *only* currently selected table metadata.
    It never reads credentials or any customer rows.
    """

    try:
        source = await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        if source.source_type is not SourceType.POSTGRESQL:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        snapshot = (
            await session.execute(
                select(CatalogSnapshot)
                .join(
                    SourceCatalogHead,
                    (SourceCatalogHead.source_id == CatalogSnapshot.source_id)
                    & (SourceCatalogHead.snapshot_id == CatalogSnapshot.id),
                )
                .where(SourceCatalogHead.source_id == source.id)
            )
        ).scalar_one_or_none()
        if snapshot is None:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        run = (
            await session.execute(
                select(CatalogSyncRun).where(
                    CatalogSyncRun.id == snapshot.sync_run_id,
                    CatalogSyncRun.source_id == source.id,
                )
            )
        ).scalar_one_or_none()
        current_scope = await _current_scope(session, source.id)
        if (
            run is None
            or run.status is not CatalogSyncRunStatus.SUCCEEDED
            or not current_scope
        ):
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        current_scope_fingerprint = canonical_scope_fingerprint(current_scope)
        if snapshot.scope_fingerprint != current_scope_fingerprint:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()

        # Source onboarding caps selected tables.  Keep the safety bound here
        # too so this helper remains safe if called by future internal paths.
        assets = (
            await session.execute(
                select(CatalogAsset)
                .join(SourceTableScope, _scope_asset_join())
                .where(
                    CatalogAsset.source_id == source.id,
                    CatalogAsset.snapshot_id == snapshot.id,
                )
                .order_by(
                    CatalogAsset.schema_name,
                    CatalogAsset.table_name,
                    CatalogAsset.id,
                )
                .limit(1001)
            )
        ).scalars().all()
        if not assets or len(assets) > 1000:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()
        return PostgresChatCatalogContext(
            source_id=source.id,
            snapshot=snapshot,
            assets=tuple(assets),
            current_scope_fingerprint=current_scope_fingerprint,
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def get_postgres_chat_asset_columns_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    asset_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    limit: int = 200,
) -> tuple[CatalogColumn, ...]:
    """Load a bounded selected asset's columns for a single planning attempt.

    The current-head/scope proof is deliberately repeated here rather than
    treating a previously returned context as an authorization token.  A
    source can be deleted or its scope changed between deterministic table
    selection and prompt construction.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 500:
        raise CatalogInspectionInputError("catalog column limit is invalid")
    context = await get_postgres_chat_catalog_context_for_owner(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    if context.snapshot.id != snapshot_id or not any(asset.id == asset_id for asset in context.assets):
        await _rollback_quietly(session)
        raise CatalogQueryContextError()
    try:
        columns = (
            await session.execute(
                select(CatalogColumn)
                .where(
                    CatalogColumn.source_id == source_id,
                    CatalogColumn.snapshot_id == snapshot_id,
                    CatalogColumn.asset_id == asset_id,
                )
                .order_by(CatalogColumn.ordinal_position, CatalogColumn.id)
                .limit(limit + 1)
            )
        ).scalars().all()
        if not columns or len(columns) > limit:
            await _rollback_quietly(session)
            raise CatalogQueryContextError()
        return tuple(columns)
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def list_catalog_assets_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    page: int,
    page_size: int,
    search: str | None = None,
) -> CatalogAssetPage:
    """List one explicitly pinned snapshot's assets within the *current* scope.

    The source scope is intentionally re-applied to every historical read.
    An asset removed from the current authorization scope is neither counted
    nor returned solely because an immutable old snapshot retained it.
    """

    page, page_size = _validated_inspection_page(page, page_size)
    search = _normalized_asset_search(search)
    try:
        await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        snapshot = await _catalog_snapshot_for_source_or_raise(
            session, source_id=source_id, snapshot_id=snapshot_id
        )
        current_scope = await _current_scope_keys(session, source_id=source_id)
        conditions = [
            CatalogAsset.source_id == source_id,
            CatalogAsset.snapshot_id == snapshot_id,
        ]
        if search is not None:
            pattern = _literal_ilike_pattern(search)
            conditions.append(
                or_(
                    CatalogAsset.schema_name.ilike(pattern, escape="\\"),
                    CatalogAsset.table_name.ilike(pattern, escape="\\"),
                )
            )
        total = (
            await session.execute(
                select(func.count())
                .select_from(CatalogAsset)
                .join(SourceTableScope, _scope_asset_join())
                .where(*conditions)
            )
        ).scalar_one()
        assets = (
            await session.execute(
                select(CatalogAsset)
                .join(SourceTableScope, _scope_asset_join())
                .where(*conditions)
                .order_by(
                    CatalogAsset.schema_name,
                    CatalogAsset.table_name,
                    CatalogAsset.id,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return CatalogAssetPage(
            source_id=source_id,
            snapshot=snapshot,
            scope_matches_current_selection=_scope_matches_snapshot(snapshot, current_scope),
            page=page,
            page_size=page_size,
            total=int(total),
            assets=tuple(_asset_summary(asset) for asset in assets),
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def _load_catalog_asset_detail(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot: CatalogSnapshot,
    asset: CatalogAsset,
    current_scope: tuple[CatalogTableIdentity, ...],
) -> CatalogAssetDetail:
    """Read a single permitted asset's normalized children in stable order."""

    columns = (
        await session.execute(
            select(CatalogColumn)
            .where(
                CatalogColumn.source_id == source_id,
                CatalogColumn.snapshot_id == snapshot.id,
                CatalogColumn.asset_id == asset.id,
            )
            .order_by(CatalogColumn.ordinal_position, CatalogColumn.id)
        )
    ).scalars().all()

    key_rows = (
        await session.execute(
            select(
                CatalogDeclaredKey,
                CatalogDeclaredKeyColumn.ordinal_position,
                CatalogColumn.column_name,
            )
            .join(
                CatalogDeclaredKeyColumn,
                and_(
                    CatalogDeclaredKeyColumn.source_id == CatalogDeclaredKey.source_id,
                    CatalogDeclaredKeyColumn.snapshot_id == CatalogDeclaredKey.snapshot_id,
                    CatalogDeclaredKeyColumn.asset_id == CatalogDeclaredKey.asset_id,
                    CatalogDeclaredKeyColumn.key_id == CatalogDeclaredKey.id,
                ),
            )
            .join(
                CatalogColumn,
                and_(
                    CatalogColumn.source_id == CatalogDeclaredKeyColumn.source_id,
                    CatalogColumn.snapshot_id == CatalogDeclaredKeyColumn.snapshot_id,
                    CatalogColumn.asset_id == CatalogDeclaredKeyColumn.asset_id,
                    CatalogColumn.id == CatalogDeclaredKeyColumn.column_id,
                ),
            )
            .where(
                CatalogDeclaredKey.source_id == source_id,
                CatalogDeclaredKey.snapshot_id == snapshot.id,
                CatalogDeclaredKey.asset_id == asset.id,
            )
            .order_by(
                CatalogDeclaredKey.constraint_name,
                CatalogDeclaredKey.id,
                CatalogDeclaredKeyColumn.ordinal_position,
            )
        )
    ).all()
    key_parts: dict[uuid.UUID, tuple[CatalogDeclaredKey, list[str]]] = {}
    for key, _ordinal, column_name in key_rows:
        existing = key_parts.get(key.id)
        if existing is None:
            existing = (key, [])
            key_parts[key.id] = existing
        existing[1].append(str(column_name))
    declared_keys = tuple(
        CatalogDeclaredKeyMetadata(
            constraint_name=key.constraint_name,
            key_kind=key.key_kind,
            column_names=tuple(column_names),
        )
        for key, column_names in key_parts.values()
    )

    target_asset = aliased(CatalogAsset)
    foreign_key_rows = (
        await session.execute(
            select(CatalogForeignKey, target_asset)
            .outerjoin(
                target_asset,
                and_(
                    target_asset.source_id == CatalogForeignKey.source_id,
                    target_asset.snapshot_id == CatalogForeignKey.snapshot_id,
                    target_asset.id == CatalogForeignKey.referenced_asset_id,
                ),
            )
            .where(
                CatalogForeignKey.source_id == source_id,
                CatalogForeignKey.snapshot_id == snapshot.id,
                CatalogForeignKey.referencing_asset_id == asset.id,
            )
            .order_by(CatalogForeignKey.constraint_name, CatalogForeignKey.id)
        )
    ).all()
    foreign_key_ids = [foreign_key.id for foreign_key, _target in foreign_key_rows]
    mappings_by_foreign_key: dict[uuid.UUID, list[CatalogForeignKeyColumnMapping]] = {}
    if foreign_key_ids:
        referencing_column = aliased(CatalogColumn)
        referenced_column = aliased(CatalogColumn)
        mapping_rows = (
            await session.execute(
                select(
                    CatalogForeignKeyColumn.foreign_key_id,
                    CatalogForeignKeyColumn.ordinal_position,
                    referencing_column.column_name,
                    referenced_column.column_name,
                )
                .join(
                    referencing_column,
                    and_(
                        referencing_column.source_id == CatalogForeignKeyColumn.source_id,
                        referencing_column.snapshot_id == CatalogForeignKeyColumn.snapshot_id,
                        referencing_column.asset_id
                        == CatalogForeignKeyColumn.referencing_asset_id,
                        referencing_column.id == CatalogForeignKeyColumn.referencing_column_id,
                    ),
                )
                .join(
                    referenced_column,
                    and_(
                        referenced_column.source_id == CatalogForeignKeyColumn.source_id,
                        referenced_column.snapshot_id == CatalogForeignKeyColumn.snapshot_id,
                        referenced_column.asset_id
                        == CatalogForeignKeyColumn.referenced_asset_id,
                        referenced_column.id == CatalogForeignKeyColumn.referenced_column_id,
                    ),
                )
                .where(
                    CatalogForeignKeyColumn.source_id == source_id,
                    CatalogForeignKeyColumn.snapshot_id == snapshot.id,
                    CatalogForeignKeyColumn.foreign_key_id.in_(foreign_key_ids),
                )
                .order_by(
                    CatalogForeignKeyColumn.foreign_key_id,
                    CatalogForeignKeyColumn.ordinal_position,
                )
            )
        ).all()
        for foreign_key_id, ordinal_position, referencing_name, referenced_name in mapping_rows:
            mappings_by_foreign_key.setdefault(foreign_key_id, []).append(
                CatalogForeignKeyColumnMapping(
                    ordinal_position=ordinal_position,
                    referencing_column_name=str(referencing_name),
                    referenced_column_name=str(referenced_name),
                )
            )

    permitted_scope = {(item.schema_name, item.table_name) for item in current_scope}
    foreign_keys: list[CatalogForeignKeyMetadata] = []
    for foreign_key, target in foreign_key_rows:
        target_is_permitted = (
            foreign_key.target_status is CatalogForeignKeyTargetStatus.IN_SCOPE
            and target is not None
            and _scope_key_from_asset(target) in permitted_scope
        )
        if target_is_permitted:
            foreign_keys.append(
                CatalogForeignKeyMetadata(
                    constraint_name=foreign_key.constraint_name,
                    target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                    referenced_asset=_asset_reference(target),
                    column_mappings=tuple(mappings_by_foreign_key.get(foreign_key.id, [])),
                )
            )
        else:
            # A historical relationship may have been in scope when captured
            # but its target can no longer be named after the owner narrows
            # the saved scope. Present the established omission indicator,
            # never the old target identity or mapping.
            foreign_keys.append(
                CatalogForeignKeyMetadata(
                    constraint_name=foreign_key.constraint_name,
                    target_status=CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE,
                    referenced_asset=None,
                    column_mappings=(),
                )
            )
    return CatalogAssetDetail(
        source_id=source_id,
        snapshot=snapshot,
        scope_matches_current_selection=_scope_matches_snapshot(snapshot, current_scope),
        asset=_asset_summary(asset),
        columns=tuple(_column_metadata(column) for column in columns),
        declared_keys=declared_keys,
        foreign_keys=tuple(foreign_keys),
    )


async def get_catalog_asset_detail_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    asset_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogAssetDetail:
    """Return one explicitly pinned, currently authorized catalog asset."""

    try:
        await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        snapshot = await _catalog_snapshot_for_source_or_raise(
            session, source_id=source_id, snapshot_id=snapshot_id
        )
        current_scope = await _current_scope_keys(session, source_id=source_id)
        asset = (
            await session.execute(
                select(CatalogAsset)
                .join(SourceTableScope, _scope_asset_join())
                .where(
                    CatalogAsset.id == asset_id,
                    CatalogAsset.source_id == source_id,
                    CatalogAsset.snapshot_id == snapshot_id,
                )
            )
        ).scalar_one_or_none()
        if asset is None:
            await _rollback_quietly(session)
            raise NotFoundError("catalog asset not found")
        return await _load_catalog_asset_detail(
            session,
            source_id=source_id,
            snapshot=snapshot,
            asset=asset,
            current_scope=current_scope,
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


async def _preceding_catalog_snapshot(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot: CatalogSnapshot,
) -> CatalogSnapshot | None:
    current_sequence = (
        await session.execute(
            select(CatalogSyncRun.request_sequence).where(
                CatalogSyncRun.source_id == source_id,
                CatalogSyncRun.id == snapshot.sync_run_id,
            )
        )
    ).scalar_one_or_none()
    if current_sequence is None:
        # The composite snapshot FK makes this unreachable without database
        # corruption. Do not manufacture a comparison from incomplete state.
        raise CatalogPersistenceError()
    return (
        await session.execute(
            select(CatalogSnapshot)
            .join(
                CatalogSyncRun,
                and_(
                    CatalogSyncRun.source_id == CatalogSnapshot.source_id,
                    CatalogSyncRun.id == CatalogSnapshot.sync_run_id,
                ),
            )
            .where(
                CatalogSnapshot.source_id == source_id,
                CatalogSyncRun.request_sequence < current_sequence,
            )
            .order_by(CatalogSyncRun.request_sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load_snapshot_asset_states(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    permitted_scope: set[tuple[str, str]],
) -> dict[tuple[str, str], _CatalogAssetState]:
    """Load one safe structural state in a bounded set of metadata queries."""

    assets = (
        await session.execute(
            select(CatalogAsset)
            .where(
                CatalogAsset.source_id == source_id,
                CatalogAsset.snapshot_id == snapshot_id,
            )
            .order_by(CatalogAsset.schema_name, CatalogAsset.table_name, CatalogAsset.id)
        )
    ).scalars().all()
    permitted_assets = [asset for asset in assets if _scope_key_from_asset(asset) in permitted_scope]
    asset_ids = [asset.id for asset in permitted_assets]
    if not asset_ids:
        return {}

    column_rows = (
        await session.execute(
            select(CatalogColumn)
            .where(
                CatalogColumn.source_id == source_id,
                CatalogColumn.snapshot_id == snapshot_id,
                CatalogColumn.asset_id.in_(asset_ids),
            )
            .order_by(CatalogColumn.asset_id, CatalogColumn.ordinal_position, CatalogColumn.id)
        )
    ).scalars().all()
    columns_by_asset: dict[uuid.UUID, list[CatalogColumnMetadata]] = {}
    for column in column_rows:
        columns_by_asset.setdefault(column.asset_id, []).append(_column_metadata(column))

    key_rows = (
        await session.execute(
            select(
                CatalogDeclaredKey,
                CatalogDeclaredKeyColumn.ordinal_position,
                CatalogColumn.column_name,
            )
            .join(
                CatalogDeclaredKeyColumn,
                and_(
                    CatalogDeclaredKeyColumn.source_id == CatalogDeclaredKey.source_id,
                    CatalogDeclaredKeyColumn.snapshot_id == CatalogDeclaredKey.snapshot_id,
                    CatalogDeclaredKeyColumn.asset_id == CatalogDeclaredKey.asset_id,
                    CatalogDeclaredKeyColumn.key_id == CatalogDeclaredKey.id,
                ),
            )
            .join(
                CatalogColumn,
                and_(
                    CatalogColumn.source_id == CatalogDeclaredKeyColumn.source_id,
                    CatalogColumn.snapshot_id == CatalogDeclaredKeyColumn.snapshot_id,
                    CatalogColumn.asset_id == CatalogDeclaredKeyColumn.asset_id,
                    CatalogColumn.id == CatalogDeclaredKeyColumn.column_id,
                ),
            )
            .where(
                CatalogDeclaredKey.source_id == source_id,
                CatalogDeclaredKey.snapshot_id == snapshot_id,
                CatalogDeclaredKey.asset_id.in_(asset_ids),
            )
            .order_by(
                CatalogDeclaredKey.asset_id,
                CatalogDeclaredKey.constraint_name,
                CatalogDeclaredKey.id,
                CatalogDeclaredKeyColumn.ordinal_position,
            )
        )
    ).all()
    key_parts: dict[uuid.UUID, tuple[CatalogDeclaredKey, list[str]]] = {}
    for key, _ordinal, column_name in key_rows:
        existing = key_parts.get(key.id)
        if existing is None:
            existing = (key, [])
            key_parts[key.id] = existing
        existing[1].append(str(column_name))
    keys_by_asset: dict[uuid.UUID, list[CatalogDeclaredKeyMetadata]] = {}
    for key, column_names in key_parts.values():
        keys_by_asset.setdefault(key.asset_id, []).append(
            CatalogDeclaredKeyMetadata(
                constraint_name=key.constraint_name,
                key_kind=key.key_kind,
                column_names=tuple(column_names),
            )
        )

    target_asset = aliased(CatalogAsset)
    foreign_key_rows = (
        await session.execute(
            select(CatalogForeignKey, target_asset)
            .outerjoin(
                target_asset,
                and_(
                    target_asset.source_id == CatalogForeignKey.source_id,
                    target_asset.snapshot_id == CatalogForeignKey.snapshot_id,
                    target_asset.id == CatalogForeignKey.referenced_asset_id,
                ),
            )
            .where(
                CatalogForeignKey.source_id == source_id,
                CatalogForeignKey.snapshot_id == snapshot_id,
                CatalogForeignKey.referencing_asset_id.in_(asset_ids),
            )
            .order_by(
                CatalogForeignKey.referencing_asset_id,
                CatalogForeignKey.constraint_name,
                CatalogForeignKey.id,
            )
        )
    ).all()
    foreign_key_ids = [foreign_key.id for foreign_key, _target in foreign_key_rows]
    mappings_by_foreign_key: dict[uuid.UUID, list[CatalogForeignKeyColumnMapping]] = {}
    if foreign_key_ids:
        referencing_column = aliased(CatalogColumn)
        referenced_column = aliased(CatalogColumn)
        mapping_rows = (
            await session.execute(
                select(
                    CatalogForeignKeyColumn.foreign_key_id,
                    CatalogForeignKeyColumn.ordinal_position,
                    referencing_column.column_name,
                    referenced_column.column_name,
                )
                .join(
                    referencing_column,
                    and_(
                        referencing_column.source_id == CatalogForeignKeyColumn.source_id,
                        referencing_column.snapshot_id == CatalogForeignKeyColumn.snapshot_id,
                        referencing_column.asset_id
                        == CatalogForeignKeyColumn.referencing_asset_id,
                        referencing_column.id == CatalogForeignKeyColumn.referencing_column_id,
                    ),
                )
                .join(
                    referenced_column,
                    and_(
                        referenced_column.source_id == CatalogForeignKeyColumn.source_id,
                        referenced_column.snapshot_id == CatalogForeignKeyColumn.snapshot_id,
                        referenced_column.asset_id
                        == CatalogForeignKeyColumn.referenced_asset_id,
                        referenced_column.id == CatalogForeignKeyColumn.referenced_column_id,
                    ),
                )
                .where(
                    CatalogForeignKeyColumn.source_id == source_id,
                    CatalogForeignKeyColumn.snapshot_id == snapshot_id,
                    CatalogForeignKeyColumn.foreign_key_id.in_(foreign_key_ids),
                )
                .order_by(
                    CatalogForeignKeyColumn.foreign_key_id,
                    CatalogForeignKeyColumn.ordinal_position,
                )
            )
        ).all()
        for foreign_key_id, ordinal_position, referencing_name, referenced_name in mapping_rows:
            mappings_by_foreign_key.setdefault(foreign_key_id, []).append(
                CatalogForeignKeyColumnMapping(
                    ordinal_position=ordinal_position,
                    referencing_column_name=str(referencing_name),
                    referenced_column_name=str(referenced_name),
                )
            )

    foreign_keys_by_asset: dict[uuid.UUID, list[CatalogForeignKeyMetadata]] = {}
    for foreign_key, target in foreign_key_rows:
        target_is_permitted = (
            foreign_key.target_status is CatalogForeignKeyTargetStatus.IN_SCOPE
            and target is not None
            and _scope_key_from_asset(target) in permitted_scope
        )
        if target_is_permitted:
            metadata = CatalogForeignKeyMetadata(
                constraint_name=foreign_key.constraint_name,
                target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                referenced_asset=_asset_reference(target),
                column_mappings=tuple(mappings_by_foreign_key.get(foreign_key.id, [])),
            )
        else:
            metadata = CatalogForeignKeyMetadata(
                constraint_name=foreign_key.constraint_name,
                target_status=CatalogForeignKeyTargetStatus.TARGET_OUT_OF_SCOPE,
                referenced_asset=None,
                column_mappings=(),
            )
        foreign_keys_by_asset.setdefault(foreign_key.referencing_asset_id, []).append(metadata)

    states: dict[tuple[str, str], _CatalogAssetState] = {}
    for asset in permitted_assets:
        states[_scope_key_from_asset(asset)] = _CatalogAssetState(
            asset=_asset_summary(asset),
            columns=tuple(columns_by_asset.get(asset.id, [])),
            declared_keys=tuple(keys_by_asset.get(asset.id, [])),
            foreign_keys=tuple(foreign_keys_by_asset.get(asset.id, [])),
        )
    return states


def _key_signature(key: CatalogDeclaredKeyMetadata) -> tuple[str, str, tuple[str, ...]]:
    return (key.constraint_name, key.key_kind.value, key.column_names)


def _foreign_key_signature(
    foreign_key: CatalogForeignKeyMetadata,
) -> tuple[
    str,
    str,
    tuple[str, str] | None,
    tuple[tuple[int, str, str], ...],
]:
    target_identity = (
        (foreign_key.referenced_asset.schema_name, foreign_key.referenced_asset.table_name)
        if foreign_key.referenced_asset is not None
        else None
    )
    mappings = tuple(
        (
            mapping.ordinal_position,
            mapping.referencing_column_name,
            mapping.referenced_column_name,
        )
        for mapping in foreign_key.column_mappings
    )
    return (foreign_key.constraint_name, foreign_key.target_status.value, target_identity, mappings)


def _build_catalog_structural_diff(
    *,
    source_id: uuid.UUID,
    snapshot: CatalogSnapshot,
    previous_snapshot: CatalogSnapshot,
    previous: dict[tuple[str, str], _CatalogAssetState],
    current: dict[tuple[str, str], _CatalogAssetState],
    change_limit: int = _CATALOG_DIFF_CHANGE_LIMIT,
) -> CatalogStructuralDiff:
    """Compare safe normalized metadata without heuristic rename detection."""

    tables_added: list[CatalogTableChange] = []
    tables_removed: list[CatalogTableChange] = []
    columns_added: list[CatalogColumnIdentityChange] = []
    columns_removed: list[CatalogColumnIdentityChange] = []
    columns_changed: list[CatalogColumnChange] = []
    keys_changed: list[CatalogKeyChange] = []
    foreign_keys_changed: list[CatalogForeignKeyChange] = []
    comments_changed: list[CatalogCommentChange] = []
    remaining = change_limit
    truncated = False

    def add(destination: list, value: object) -> None:
        nonlocal remaining, truncated
        if remaining <= 0:
            truncated = True
            return
        destination.append(value)
        remaining -= 1

    previous_tables = set(previous)
    current_tables = set(current)
    for schema_name, table_name in sorted(current_tables - previous_tables):
        add(tables_added, CatalogTableChange(schema_name=schema_name, table_name=table_name))
    for schema_name, table_name in sorted(previous_tables - current_tables):
        add(tables_removed, CatalogTableChange(schema_name=schema_name, table_name=table_name))

    for identity in sorted(previous_tables & current_tables):
        previous_asset = previous[identity]
        current_asset = current[identity]
        schema_name, table_name = identity
        previous_columns = {column.column_name: column for column in previous_asset.columns}
        current_columns = {column.column_name: column for column in current_asset.columns}
        for column_name in sorted(current_columns.keys() - previous_columns.keys()):
            add(
                columns_added,
                CatalogColumnIdentityChange(
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=column_name,
                ),
            )
        for column_name in sorted(previous_columns.keys() - current_columns.keys()):
            add(
                columns_removed,
                CatalogColumnIdentityChange(
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=column_name,
                ),
            )
        for column_name in sorted(previous_columns.keys() & current_columns.keys()):
            before = previous_columns[column_name]
            after = current_columns[column_name]
            if (
                before.formatted_native_type != after.formatted_native_type
                or before.is_nullable != after.is_nullable
            ):
                add(
                    columns_changed,
                    CatalogColumnChange(
                        schema_name=schema_name,
                        table_name=table_name,
                        column_name=column_name,
                        previous_formatted_native_type=before.formatted_native_type,
                        current_formatted_native_type=after.formatted_native_type,
                        previous_is_nullable=before.is_nullable,
                        current_is_nullable=after.is_nullable,
                    ),
                )

        previous_keys = {_key_signature(key): key for key in previous_asset.declared_keys}
        current_keys = {_key_signature(key): key for key in current_asset.declared_keys}
        for signature in sorted(current_keys.keys() - previous_keys.keys()):
            key = current_keys[signature]
            add(
                keys_changed,
                CatalogKeyChange(
                    change="added",
                    schema_name=schema_name,
                    table_name=table_name,
                    constraint_name=key.constraint_name,
                    key_kind=key.key_kind,
                    column_names=key.column_names,
                ),
            )
        for signature in sorted(previous_keys.keys() - current_keys.keys()):
            key = previous_keys[signature]
            add(
                keys_changed,
                CatalogKeyChange(
                    change="removed",
                    schema_name=schema_name,
                    table_name=table_name,
                    constraint_name=key.constraint_name,
                    key_kind=key.key_kind,
                    column_names=key.column_names,
                ),
            )

        previous_foreign_keys = {
            _foreign_key_signature(foreign_key): foreign_key
            for foreign_key in previous_asset.foreign_keys
        }
        current_foreign_keys = {
            _foreign_key_signature(foreign_key): foreign_key
            for foreign_key in current_asset.foreign_keys
        }
        for signature in sorted(current_foreign_keys.keys() - previous_foreign_keys.keys()):
            foreign_key = current_foreign_keys[signature]
            add(
                foreign_keys_changed,
                CatalogForeignKeyChange(
                    change="added",
                    schema_name=schema_name,
                    table_name=table_name,
                    constraint_name=foreign_key.constraint_name,
                    target_status=foreign_key.target_status,
                    referenced_asset=foreign_key.referenced_asset,
                    column_mappings=foreign_key.column_mappings,
                ),
            )
        for signature in sorted(previous_foreign_keys.keys() - current_foreign_keys.keys()):
            foreign_key = previous_foreign_keys[signature]
            add(
                foreign_keys_changed,
                CatalogForeignKeyChange(
                    change="removed",
                    schema_name=schema_name,
                    table_name=table_name,
                    constraint_name=foreign_key.constraint_name,
                    target_status=foreign_key.target_status,
                    referenced_asset=foreign_key.referenced_asset,
                    column_mappings=foreign_key.column_mappings,
                ),
            )

        if previous_asset.asset.table_comment != current_asset.asset.table_comment:
            add(
                comments_changed,
                CatalogCommentChange(
                    subject="table",
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=None,
                    previous_comment=previous_asset.asset.table_comment,
                    current_comment=current_asset.asset.table_comment,
                ),
            )
        for column_name in sorted(previous_columns.keys() & current_columns.keys()):
            before = previous_columns[column_name]
            after = current_columns[column_name]
            if before.column_comment != after.column_comment:
                add(
                    comments_changed,
                    CatalogCommentChange(
                        subject="column",
                        schema_name=schema_name,
                        table_name=table_name,
                        column_name=column_name,
                        previous_comment=before.column_comment,
                        current_comment=after.column_comment,
                    ),
                )

    return CatalogStructuralDiff(
        source_id=source_id,
        snapshot=snapshot,
        state="compared",
        previous_snapshot=previous_snapshot,
        truncated=truncated,
        change_limit=change_limit,
        tables_added=tuple(tables_added),
        tables_removed=tuple(tables_removed),
        columns_added=tuple(columns_added),
        columns_removed=tuple(columns_removed),
        columns_changed=tuple(columns_changed),
        keys_changed=tuple(keys_changed),
        foreign_keys_changed=tuple(foreign_keys_changed),
        comments_changed=tuple(comments_changed),
    )


async def get_catalog_structural_diff_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> CatalogStructuralDiff:
    """Compare a pinned snapshot to its preceding publication when safe.

    Any captured/current scope mismatch is deliberately a non-comparison. It
    avoids asserting that an unselected table was dropped and prevents older
    out-of-scope metadata from entering a diff response.
    """

    try:
        await _owned_source_or_raise(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        snapshot = await _catalog_snapshot_for_source_or_raise(
            session, source_id=source_id, snapshot_id=snapshot_id
        )
        current_scope = await _current_scope_keys(session, source_id=source_id)
        permitted_scope = {(item.schema_name, item.table_name) for item in current_scope}
        if not _scope_matches_snapshot(snapshot, current_scope):
            return CatalogStructuralDiff(
                source_id=source_id,
                snapshot=snapshot,
                state="scope_changed",
                previous_snapshot=None,
                truncated=False,
                change_limit=_CATALOG_DIFF_CHANGE_LIMIT,
            )
        previous_snapshot = await _preceding_catalog_snapshot(
            session, source_id=source_id, snapshot=snapshot
        )
        if previous_snapshot is None:
            return CatalogStructuralDiff(
                source_id=source_id,
                snapshot=snapshot,
                state="baseline",
                previous_snapshot=None,
                truncated=False,
                change_limit=_CATALOG_DIFF_CHANGE_LIMIT,
            )
        if previous_snapshot.scope_fingerprint != snapshot.scope_fingerprint:
            return CatalogStructuralDiff(
                source_id=source_id,
                snapshot=snapshot,
                state="scope_changed",
                previous_snapshot=None,
                truncated=False,
                change_limit=_CATALOG_DIFF_CHANGE_LIMIT,
            )
        previous = await _load_snapshot_asset_states(
            session,
            source_id=source_id,
            snapshot_id=previous_snapshot.id,
            permitted_scope=permitted_scope,
        )
        current = await _load_snapshot_asset_states(
            session,
            source_id=source_id,
            snapshot_id=snapshot.id,
            permitted_scope=permitted_scope,
        )
        return _build_catalog_structural_diff(
            source_id=source_id,
            snapshot=snapshot,
            previous_snapshot=previous_snapshot,
            previous=previous,
            current=current,
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise CatalogPersistenceError() from None


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
    "CatalogAssetDetail",
    "CatalogAssetInput",
    "CatalogAssetPage",
    "CatalogAssetReference",
    "CatalogAssetSummary",
    "CatalogAvailability",
    "CatalogColumnChange",
    "CatalogColumnIdentityChange",
    "CatalogColumnInput",
    "CatalogColumnMetadata",
    "CatalogCommentChange",
    "CatalogDeclaredKeyMetadata",
    "CatalogForeignKeyChange",
    "CatalogForeignKeyColumnMapping",
    "CatalogForeignKeyColumnInput",
    "CatalogForeignKeyInput",
    "CatalogForeignKeyMetadata",
    "CatalogInputError",
    "CatalogInspectionInputError",
    "CatalogKeyChange",
    "CatalogKeyInput",
    "CatalogPersistenceError",
    "CatalogQueryContextError",
    "CatalogRunLeaseLostError",
    "CatalogRunStateError",
    "CatalogRunSupersededError",
    "CatalogScopeChangedError",
    "CatalogSnapshotInput",
    "CatalogStructuralDiff",
    "CatalogSyncLease",
    "CatalogTableChange",
    "CatalogTableIdentity",
    "PostgresQueryCatalogContext",
    "PostgresChatCatalogContext",
    "cancel_catalog_sync_run_for_owner",
    "canonical_scope_fingerprint",
    "claim_next_catalog_sync_run",
    "fail_catalog_sync_run",
    "finalize_catalog_sync_worker_failure",
    "get_catalog_availability_for_owner",
    "get_catalog_asset_detail_for_owner",
    "get_catalog_structural_diff_for_owner",
    "get_catalog_sync_run_for_owner",
    "get_catalog_snapshot_for_owner",
    "get_current_catalog_snapshot_for_owner",
    "get_postgres_chat_asset_columns_for_owner",
    "get_postgres_chat_catalog_context_for_owner",
    "mark_catalog_sync_running",
    "list_catalog_assets_for_owner",
    "publish_catalog_snapshot",
    "resolve_postgres_query_catalog_context_for_owner",
    "renew_catalog_sync_lease",
    "request_catalog_sync_for_owner",
]
