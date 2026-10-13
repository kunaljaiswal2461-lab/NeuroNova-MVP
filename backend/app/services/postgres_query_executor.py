"""Stage 4B internal gateway for controlled PostgreSQL query execution.

The public API in this module accepts only a trusted actor/source/snapshot
context and a :class:`PostgresQueryPlan`.  It deliberately accepts neither
SQL nor a caller-supplied ``CompiledPostgresQuery``.  Each authorization and
persistence phase owns a short application-database session which is closed
before client-database I/O begins.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.connectors.postgres_query_executor import (
    ActualPostgresOutputColumn,
    LiveColumnExpectation,
    LiveForeignKeyExpectation,
    LiveRelationExpectation,
    LiveQueryExpectation,
    LiveUniqueKeyExpectation,
    PostgresExecutionAdapterError,
    PostgresExecutionCancellation,
    PostgresExecutionCancelledError,
    PostgresQueryExecutionAdapter,
    RemotePostgresExecution,
)
from app.core.config import Settings
from app.core.connector_credentials import (
    CredentialUnavailableError,
    InvalidCredentialBundleError,
)
from app.db.models.catalog import CatalogColumn, SourceCatalogHead
from app.db.models.metric_definition import MetricDefinitionVersion
from app.db.models.postgres_query_run import PostgresQueryRun, PostgresQueryRunStatus
from app.db.models.relationship_definition import RelationshipDefinitionVersion
from app.db.models.source import DataSource, SourceStatus, SourceType
from app.exceptions.custom_exceptions import NotFoundError
from app.services.catalog_service import CatalogQueryContextError, resolve_postgres_query_catalog_context_for_owner
from app.services.postgres_query_compiler import (
    CompiledPostgresQuery,
    CompiledPostgresJoin,
    ExpectedPostgresOutputColumn,
    PostgresQueryPlanValidationError,
    validate_and_compile_postgres_query,
)
from app.services.postgres_query_plan import PostgresQueryPlan
from app.services.relationship_service import RelationshipServiceError
from app.services.source_service import (
    ConnectorActivationError,
    ConnectorDisabledError,
    CredentialKeyUnavailableError,
    require_credential_operations,
)


_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_FAILURE_CODES = frozenset(
    {
        "access_changed",
        "access_revoked",
        "catalog_unavailable",
        "connector_not_available",
        "credential_unavailable",
        "execution_cancelled",
        "execution_failed",
        "execution_recording_failed",
        "execution_timeout",
        "invalid_query_plan",
        "metric_definition_changed",
        "query_error",
        "query_too_expensive",
        "result_row_too_large",
        "unsupported_output_type",
        "relationship_definition_changed",
        "query_not_authorized",
        "restricted_role_required",
        "result_shape_mismatch",
        "schema_changed",
        "source_unavailable",
        "tls_verification_failed",
        "unsupported_execution_configuration",
    }
)
class PostgresResultValue(BaseModel):
    """One explicit JSON-safe database value.

    Numeric values use canonical strings.  This prevents a PostgreSQL
    ``numeric`` or ``int8`` from silently becoming an imprecise JavaScript
    number and makes non-finite float values representable without invalid
    JSON.  A future presentation layer can deliberately choose display types.
    """

    model_config = _MODEL_CONFIG

    kind: Literal[
        "null",
        "boolean",
        "integer",
        "decimal",
        "number",
        "non_finite_number",
        "text",
        "uuid",
        "date",
        "timestamp",
        "timestamptz",
    ]
    value: str | bool | None


class PostgresQueryResultColumn(BaseModel):
    model_config = _MODEL_CONFIG

    alias: str
    logical_type: str
    expected_postgres_type: str
    actual_postgres_type: str | None = None
    source_column_id: UUID | None = None
    source_column_name: str | None = None
    operation: str | None = None
    bucket: str | None = None
    time_zone: str | None = None


class PostgresQueryOrdering(BaseModel):
    model_config = _MODEL_CONFIG

    alias: str
    direction: Literal["asc", "desc"]


class PostgresMetricProvenance(BaseModel):
    """Safe semantic facts attached to a result from a confirmed metric.

    Fixed-filter values and the definition description intentionally stay out
    of this transport/audit payload. The owner-only definition API is the
    place to inspect the complete confirmed contract.
    """

    model_config = _MODEL_CONFIG

    metric_id: UUID
    version_number: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    aggregation: Literal["count_all", "count_column", "sum", "average", "minimum", "maximum"]
    aggregation_column_name: str | None = None
    unit_label: str | None = Field(default=None, max_length=64)
    default_date_column_name: str | None = None
    default_time_zone: str | None = Field(default=None, max_length=128)
    fixed_filter_count: int = Field(ge=0)


class PostgresRelationshipProvenance(BaseModel):
    """Safe facts for one compiler-authorized same-source join."""

    model_config = _MODEL_CONFIG

    relationship_id: UUID
    relationship_version_id: UUID
    version_number: int = Field(ge=1)
    join_type: Literal["inner", "left"]


@dataclass(frozen=True)
class PostgresQueryDeliveryBinding:
    """Internal source fence carried only until chat result delivery.

    It is deliberately excluded from the typed result transport and transcript
    JSON.  The executor captures it after its final remote-work revalidation;
    chat uses it to serialize the row-bearing transcript write and SSE frame
    with saved-source configuration changes.
    """

    owner_user_id: UUID
    source_id: UUID
    snapshot_id: UUID
    configuration_generation: int
    required_relations: tuple[tuple[str, str], ...]


class PostgresQueryExecutionResult(BaseModel):
    """A typed, bounded result for a successful or failed Stage 4B run."""

    model_config = _MODEL_CONFIG

    execution_id: UUID
    status: Literal["succeeded", "failed", "cancelled"]
    source_id: UUID
    snapshot_id: UUID
    asset_id: UUID | None = None
    columns: tuple[PostgresQueryResultColumn, ...] = ()
    rows: tuple[tuple[PostgresResultValue, ...], ...] = ()
    # This is the visible count, not an unbounded database total.
    returned_row_count: int = Field(default=0, ge=0)
    requested_limit: int | None = Field(default=None, ge=1)
    safety_row_limit: int | None = Field(default=None, ge=1)
    safety_byte_limit: int | None = Field(default=None, ge=1)
    serialized_row_bytes: int = Field(default=0, ge=0)
    safety_row_truncated: bool = False
    safety_byte_truncated: bool = False
    complete: bool = False
    explicit_top_n: bool = False
    ordering: tuple[PostgresQueryOrdering, ...] = ()
    calculation_semantics: dict[str, Any] = Field(default_factory=dict)
    metric_provenance: PostgresMetricProvenance | None = None
    relationship_provenance: PostgresRelationshipProvenance | None = None
    session_time_zone: str = "UTC"
    warnings: tuple[str, ...] = ()
    failure_code: str | None = None
    # Never serialize this implementation-only fence into SSE, persisted chat
    # rows, API models, logs, or model prompts.  It exists only while a
    # completed result crosses from the executor to the chat delivery guard.
    delivery_binding: PostgresQueryDeliveryBinding | None = Field(
        default=None, exclude=True, repr=False
    )
    started_at: datetime
    completed_at: datetime
    duration_ms: float = Field(ge=0.0)

    @property
    def is_ok(self) -> bool:
        return self.status == "succeeded"

    def safe_provenance(self) -> dict[str, Any]:
        """Safe lifecycle data for telemetry; intentionally excludes rows."""

        return {
            "execution_id": str(self.execution_id),
            "status": self.status,
            "source_id": str(self.source_id),
            "snapshot_id": str(self.snapshot_id),
            "asset_id": str(self.asset_id) if self.asset_id is not None else None,
            "returned_row_count": self.returned_row_count,
            "complete": self.complete,
            "safety_row_truncated": self.safety_row_truncated,
            "safety_byte_truncated": self.safety_byte_truncated,
            "failure_code": self.failure_code,
            "metric_id": (
                str(self.metric_provenance.metric_id)
                if self.metric_provenance is not None
                else None
            ),
            "metric_version_number": (
                self.metric_provenance.version_number
                if self.metric_provenance is not None
                else None
            ),
            "metric_name": self.metric_provenance.name if self.metric_provenance is not None else None,
            "relationship_id": (
                str(self.relationship_provenance.relationship_id)
                if self.relationship_provenance is not None
                else None
            ),
            "relationship_version_number": (
                self.relationship_provenance.version_number
                if self.relationship_provenance is not None
                else None
            ),
            "relationship_version_id": (
                str(self.relationship_provenance.relationship_version_id)
                if self.relationship_provenance is not None
                else None
            ),
            "join_type": (
                self.relationship_provenance.join_type
                if self.relationship_provenance is not None
                else None
            ),
        }


class _SessionFactory(Protocol):
    def __call__(self) -> Any: ...


# A metric pin belongs to the Stage 5A ownership/catalog service. Keep the
# gateway decoupled from that service to avoid a service import cycle: the
# conversational caller supplies a narrowly scoped fresh-session closure.
MetricExecutionPinVerifier = Callable[[], Awaitable[bool]]
RelationshipExecutionPinVerifier = Callable[[], Awaitable[bool]]


class _ExecutionBoundaryError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ResultSerializationError(Exception):
    pass


@dataclass(frozen=True)
class _ExecutionSource:
    source_id: UUID
    configuration_generation: int
    host: str
    port: int
    database_name: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    custom_ca_pem: str | None = field(default=None, repr=False)


@dataclass
class _KeyedGate:
    semaphore: asyncio.Semaphore
    references: int = 0


class PostgresExecutionLimiter:
    """Process-local global/user/source concurrency boundary.

    It intentionally does not coordinate multiple web replicas.  Deployment
    must keep connector egress controls and add a distributed limiter before
    horizontally scaling execution traffic.
    """

    def __init__(self, *, global_limit: int, per_user_limit: int, per_source_limit: int) -> None:
        self._global = asyncio.Semaphore(global_limit)
        self._per_user_limit = per_user_limit
        self._per_source_limit = per_source_limit
        self._users: dict[UUID, _KeyedGate] = {}
        self._sources: dict[UUID, _KeyedGate] = {}
        self._lock = asyncio.Lock()

    async def _acquire_keyed(
        self, gates: dict[UUID, _KeyedGate], key: UUID, limit: int
    ) -> tuple[dict[UUID, _KeyedGate], UUID, _KeyedGate]:
        async with self._lock:
            gate = gates.get(key)
            if gate is None:
                gate = _KeyedGate(asyncio.Semaphore(limit))
                gates[key] = gate
            gate.references += 1
        try:
            await gate.semaphore.acquire()
        except BaseException:
            await self._release_keyed(gates, key, gate, acquired=False)
            raise
        return gates, key, gate

    async def _release_keyed(
        self,
        gates: dict[UUID, _KeyedGate],
        key: UUID,
        gate: _KeyedGate,
        *,
        acquired: bool,
    ) -> None:
        if acquired:
            gate.semaphore.release()
        async with self._lock:
            gate.references -= 1
            if gate.references == 0 and gates.get(key) is gate:
                del gates[key]

    @asynccontextmanager
    async def slot(self, *, owner_user_id: UUID, source_id: UUID) -> AsyncIterator[None]:
        global_acquired = False
        user_gate: tuple[dict[UUID, _KeyedGate], UUID, _KeyedGate] | None = None
        source_gate: tuple[dict[UUID, _KeyedGate], UUID, _KeyedGate] | None = None
        try:
            await self._global.acquire()
            global_acquired = True
            user_gate = await self._acquire_keyed(self._users, owner_user_id, self._per_user_limit)
            source_gate = await self._acquire_keyed(
                self._sources, source_id, self._per_source_limit
            )
            yield
        finally:
            if source_gate is not None:
                await self._release_keyed(*source_gate, acquired=True)
            if user_gate is not None:
                await self._release_keyed(*user_gate, acquired=True)
            if global_acquired:
                self._global.release()


_LIMITERS: dict[tuple[int, int, int, int], PostgresExecutionLimiter] = {}


def _limiter_for(settings: Settings) -> PostgresExecutionLimiter:
    # Uvicorn's normal worker model uses one event loop per process.  Include
    # the loop identity so disposable test loops cannot accidentally share an
    # asyncio semaphore while preserving process-local coordination in a live
    # worker.
    loop_id = id(asyncio.get_running_loop())
    key = (
        loop_id,
        settings.postgres_execution_max_concurrent_operations,
        settings.postgres_execution_max_concurrent_per_user,
        settings.postgres_execution_max_concurrent_per_source,
    )
    limiter = _LIMITERS.get(key)
    if limiter is None:
        limiter = PostgresExecutionLimiter(
            global_limit=key[1], per_user_limit=key[2], per_source_limit=key[3]
        )
        _LIMITERS[key] = limiter
    return limiter


AdapterFactory = Callable[[Settings], PostgresQueryExecutionAdapter]


def _build_adapter(settings: Settings) -> PostgresQueryExecutionAdapter:
    return PostgresQueryExecutionAdapter(settings=settings)


def _referenced_column_ids_by_asset(
    plan: PostgresQueryPlan,
    *,
    compiled_join: CompiledPostgresJoin | None,
) -> dict[UUID, tuple[UUID, ...]]:
    """Re-resolve every selected and equality-key column before remote I/O."""

    references: dict[UUID, list[UUID]] = {plan.asset_id: []}
    if compiled_join is not None:
        references[compiled_join.lookup_asset_id] = []

    def add(asset_id: UUID, column_id: UUID) -> None:
        if asset_id not in references:
            raise _ExecutionBoundaryError("catalog_unavailable")
        references[asset_id].append(column_id)

    def reference_asset_id(item: Any) -> UUID:
        asset_id = getattr(item, "asset_id", None)
        return plan.asset_id if asset_id is None else asset_id

    for item in plan.projections:
        add(reference_asset_id(item), item.column_id)
    for item in plan.filters:
        add(reference_asset_id(item), item.column_id)
    for item in plan.group_by:
        add(reference_asset_id(item), item.column_id)
    for item in plan.aggregations:
        if item.column_id is not None:
            add(reference_asset_id(item), item.column_id)
    if compiled_join is not None:
        for base_column_id, lookup_column_id in compiled_join.column_pairs:
            add(plan.asset_id, base_column_id)
            add(compiled_join.lookup_asset_id, lookup_column_id)
    return {
        asset_id: tuple(dict.fromkeys(column_ids))
        for asset_id, column_ids in references.items()
    }


def _live_columns(columns: dict[UUID, CatalogColumn]) -> tuple[LiveColumnExpectation, ...]:
    return tuple(
        LiveColumnExpectation(
            name=column.column_name,
            native_type_schema=column.native_type_schema,
            native_type_name=column.native_type_name,
            native_type_oid=column.native_type_oid,
            native_type_modifier=column.native_type_modifier,
            is_nullable=column.is_nullable,
        )
        for column in sorted(columns.values(), key=lambda value: (value.ordinal_position, value.id))
    )


def _runtime_expectation(
    *,
    compiled: CompiledPostgresQuery,
    asset: Any,
    columns: dict[UUID, CatalogColumn],
    lookup_asset: Any | None = None,
    lookup_columns: dict[UUID, CatalogColumn] | None = None,
) -> LiveQueryExpectation:
    if asset.id != compiled.asset_id:
        raise _ExecutionBoundaryError("catalog_unavailable")
    lookup_relation: LiveRelationExpectation | None = None
    foreign_key: LiveForeignKeyExpectation | None = None
    if compiled.join is not None:
        if (
            lookup_asset is None
            or lookup_columns is None
            or lookup_asset.id != compiled.join.lookup_asset_id
            or lookup_asset.schema_name != compiled.join.lookup_schema_name
            or lookup_asset.table_name != compiled.join.lookup_table_name
        ):
            raise _ExecutionBoundaryError("catalog_unavailable")
        try:
            base_column_names = tuple(
                columns[base_column_id].column_name
                for base_column_id, _lookup_column_id in compiled.join.column_pairs
            )
            lookup_column_names = tuple(
                lookup_columns[lookup_column_id].column_name
                for _base_column_id, lookup_column_id in compiled.join.column_pairs
            )
        except KeyError as exc:
            raise _ExecutionBoundaryError("catalog_unavailable") from exc
        if (
            not base_column_names
            or base_column_names != compiled.join.foreign_key_base_column_names
            or lookup_column_names != compiled.join.foreign_key_lookup_column_names
            or not compiled.join.foreign_key_constraint_name
        ):
            raise _ExecutionBoundaryError("catalog_unavailable")
        lookup_relation = LiveRelationExpectation(
            schema_name=lookup_asset.schema_name,
            table_name=lookup_asset.table_name,
            columns=_live_columns(lookup_columns),
            unique_key=LiveUniqueKeyExpectation(
                constraint_name=compiled.join.target_key_constraint_name,
                key_kind=compiled.join.target_key_kind,
                column_names=compiled.join.target_key_column_names,
            ),
        )
        foreign_key = LiveForeignKeyExpectation(
            constraint_name=compiled.join.foreign_key_constraint_name,
            base_column_names=base_column_names,
            lookup_column_names=lookup_column_names,
        )
    elif lookup_asset is not None or lookup_columns is not None:
        raise _ExecutionBoundaryError("catalog_unavailable")
    return LiveQueryExpectation(
        schema_name=asset.schema_name,
        table_name=asset.table_name,
        columns=_live_columns(columns),
        expected_output_columns=compiled.expected_output_columns,
        lookup_relation=lookup_relation,
        foreign_key=foreign_key,
    )


async def _authorize_and_resolve(
    session_factory: _SessionFactory,
    *,
    owner_user_id: UUID,
    source_id: UUID,
    snapshot_id: UUID,
    plan: PostgresQueryPlan,
    settings: Settings,
) -> tuple[CompiledPostgresQuery, LiveQueryExpectation]:
    """Use a short app-db session and close its read transaction before I/O."""

    async with session_factory() as session:
        try:
            compiled = await validate_and_compile_postgres_query(
                session,
                owner_user_id=owner_user_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                plan=plan,
                settings=settings,
            )
            references_by_asset = _referenced_column_ids_by_asset(
                plan,
                compiled_join=compiled.join,
            )
            context = await resolve_postgres_query_catalog_context_for_owner(
                session,
                owner_user_id=owner_user_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                asset_id=plan.asset_id,
                column_ids=references_by_asset[plan.asset_id],
            )
            lookup_context = None
            if compiled.join is not None:
                lookup_context = await resolve_postgres_query_catalog_context_for_owner(
                    session,
                    owner_user_id=owner_user_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    asset_id=compiled.join.lookup_asset_id,
                    column_ids=references_by_asset[compiled.join.lookup_asset_id],
                )
                if (
                    lookup_context.source.id != context.source.id
                    or lookup_context.snapshot.id != context.snapshot.id
                    or lookup_context.current_scope_fingerprint
                    != context.current_scope_fingerprint
                ):
                    raise _ExecutionBoundaryError("catalog_unavailable")
            expectation = _runtime_expectation(
                compiled=compiled,
                asset=context.asset,
                columns=dict(context.columns),
                lookup_asset=lookup_context.asset if lookup_context is not None else None,
                lookup_columns=(dict(lookup_context.columns) if lookup_context is not None else None),
            )
        finally:
            # SQLAlchemy's async session begins a transaction for the read
            # queries above.  Never let it span a client connection attempt.
            await _rollback_quietly(session)
    return compiled, expectation


async def _load_execution_source(
    session_factory: _SessionFactory,
    *,
    owner_user_id: UUID,
    source_id: UUID,
    expectation: LiveQueryExpectation | None = None,
    settings: Settings,
    required_relations: frozenset[tuple[str, str]] | None = None,
) -> _ExecutionSource:
    """``expectation`` (compiled path) or ``required_relations`` (validated-SQL path) names the tables read."""
    try:
        protector = require_credential_operations(settings)
    except (ConnectorDisabledError, ConnectorActivationError, CredentialKeyUnavailableError):
        raise _ExecutionBoundaryError("connector_not_available") from None

    async with session_factory() as session:
        try:
            source = (
                await session.execute(
                    select(DataSource)
                    .where(
                        DataSource.id == source_id,
                        DataSource.owner_user_id == owner_user_id,
                    )
                    .options(selectinload(DataSource.credential), selectinload(DataSource.table_scopes))
                )
            ).scalar_one_or_none()
            needed_relations = (
                set(required_relations)
                if required_relations is not None
                else {
                    (relation.schema_name, relation.table_name)
                    for relation in expectation.relation_expectations()
                }
            )
            permitted_relations = {
                (scope.schema_name, scope.table_name) for scope in source.table_scopes
            } if source is not None else set()
            if (
                source is None
                or source.source_type is not SourceType.POSTGRESQL
                or source.status is not SourceStatus.TESTED
                or source.credential is None
                or not needed_relations.issubset(permitted_relations)
            ):
                raise _ExecutionBoundaryError("source_unavailable")
            try:
                bundle = protector.decrypt(
                    source_id=source.id,
                    ciphertext=source.credential.ciphertext,
                    key_id=source.credential.key_id,
                    encryption_format=source.credential.encryption_format,
                )
            except (CredentialUnavailableError, InvalidCredentialBundleError):
                raise _ExecutionBoundaryError("credential_unavailable") from None
            return _ExecutionSource(
                source_id=source.id,
                configuration_generation=getattr(source, "configuration_generation", 0),
                host=source.host,
                port=source.port,
                database_name=source.database_name,
                username=bundle.username,
                password=bundle.password,
                custom_ca_pem=bundle.custom_ca_pem,
            )
        except SQLAlchemyError:
            raise _ExecutionBoundaryError("catalog_unavailable") from None
        finally:
            await _rollback_quietly(session)


async def _verify_delivery_source_access(
    session_factory: _SessionFactory,
    *,
    owner_user_id: UUID,
    source_id: UUID,
    expectation: LiveQueryExpectation,
    expected_configuration_generation: int,
    settings: Settings,
) -> None:
    """Fail closed if source activation or scope changed during remote work.

    This intentionally does not decrypt credentials: its sole purpose is to
    ensure a completed client query is still disclosable to the owner.
    """

    try:
        require_credential_operations(settings)
    except (ConnectorDisabledError, ConnectorActivationError, CredentialKeyUnavailableError):
        raise _ExecutionBoundaryError("access_revoked") from None

    async with session_factory() as session:
        try:
            source = (
                await session.execute(
                    select(DataSource)
                    .where(
                        DataSource.id == source_id,
                        DataSource.owner_user_id == owner_user_id,
                    )
                    .options(selectinload(DataSource.credential), selectinload(DataSource.table_scopes))
                )
            ).scalar_one_or_none()
            required_relations = {
                (relation.schema_name, relation.table_name)
                for relation in expectation.relation_expectations()
            }
            permitted_relations = {
                (scope.schema_name, scope.table_name) for scope in source.table_scopes
            } if source is not None else set()
            if (
                source is None
                or source.source_type is not SourceType.POSTGRESQL
                or source.status is not SourceStatus.TESTED
                or source.credential is None
                or source.configuration_generation != expected_configuration_generation
                or not required_relations.issubset(permitted_relations)
            ):
                raise _ExecutionBoundaryError("access_revoked")
        except _ExecutionBoundaryError:
            raise
        except SQLAlchemyError:
            raise _ExecutionBoundaryError("catalog_unavailable") from None
        finally:
            await _rollback_quietly(session)


async def _verify_locked_delivery_binding(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    binding: PostgresQueryDeliveryBinding,
    settings: Settings,
) -> None:
    """Lock and validate the saved-source facts that authorize result delivery.

    Source maintenance mutations acquire the same ``DataSource`` row lock
    before replacing scope or credentials.  Keeping this check and a caller's
    transcript write in one transaction gives either operation a clear order:
    a prior mutation makes the binding fail, while a later scope removal sees
    and clears a committed result blob.
    """

    if binding.owner_user_id != owner_user_id:
        raise _ExecutionBoundaryError("access_revoked")
    try:
        require_credential_operations(settings)
    except (ConnectorDisabledError, ConnectorActivationError, CredentialKeyUnavailableError):
        raise _ExecutionBoundaryError("access_revoked") from None

    try:
        source = (
            await session.execute(
                select(DataSource)
                .where(
                    DataSource.id == binding.source_id,
                    DataSource.owner_user_id == owner_user_id,
                )
                .options(selectinload(DataSource.credential), selectinload(DataSource.table_scopes))
                .with_for_update()
            )
        ).scalar_one_or_none()
        permitted_relations = {
            (scope.schema_name, scope.table_name) for scope in source.table_scopes
        } if source is not None else set()
        # Catalog publication takes this same source-row lock.  Once it is
        # held, the head cannot change until the caller commits/releases the
        # delivery critical section.  A newer head that committed first makes
        # the executor's pinned snapshot historical and must withhold rows.
        head = await session.get(SourceCatalogHead, binding.source_id)
        if (
            source is None
            or source.source_type is not SourceType.POSTGRESQL
            or source.status is not SourceStatus.TESTED
            or source.credential is None
            or source.configuration_generation != binding.configuration_generation
            or head is None
            or head.snapshot_id != binding.snapshot_id
            or not set(binding.required_relations).issubset(permitted_relations)
        ):
            raise _ExecutionBoundaryError("access_revoked")
    except _ExecutionBoundaryError:
        raise
    except SQLAlchemyError:
        raise _ExecutionBoundaryError("catalog_unavailable") from None


@asynccontextmanager
async def postgres_query_delivery_guard(
    session_factory: _SessionFactory,
    *,
    owner_user_id: UUID,
    binding: PostgresQueryDeliveryBinding,
    settings: Settings,
) -> AsyncIterator[AsyncSession]:
    """Hold the saved-source delivery fence across one local critical section.

    Callers may persist a transcript result or yield its SSE frame while this
    context is active.  It deliberately never performs remote I/O.
    """

    async with session_factory() as session:
        try:
            await _verify_locked_delivery_binding(
                session,
                owner_user_id=owner_user_id,
                binding=binding,
                settings=settings,
            )
            try:
                yield session
            except BaseException:
                await _rollback_quietly(session)
                raise
            else:
                await session.commit()
        except BaseException:
            await _rollback_quietly(session)
            raise


async def _create_run(
    session_factory: _SessionFactory,
    *,
    execution_id: UUID,
    owner_user_id: UUID,
    compiled: CompiledPostgresQuery,
    started_at: datetime,
    metric_definition_version_id: UUID | None = None,
    relationship_definition_version_id: UUID | None = None,
) -> None:
    async with session_factory() as session:
        try:
            session.add(
                PostgresQueryRun(
                    id=execution_id,
                    owner_user_id=owner_user_id,
                    source_id=compiled.source_id,
                    snapshot_id=compiled.snapshot_id,
                    asset_id=compiled.asset_id,
                    plan_fingerprint=compiled.fingerprint,
                    status=PostgresQueryRunStatus.RUNNING,
                    requested_at=started_at,
                    started_at=started_at,
                    result_truncated=False,
                    result_complete=False,
                    returned_row_count=0,
                    metric_definition_version_id=metric_definition_version_id,
                    relationship_definition_version_id=relationship_definition_version_id,
                )
            )
            await session.commit()
        except SQLAlchemyError:
            await _rollback_quietly(session)
            raise _ExecutionBoundaryError("execution_recording_failed") from None


async def _finalize_run(
    session_factory: _SessionFactory,
    *,
    result: PostgresQueryExecutionResult,
) -> None:
    status = {
        "succeeded": PostgresQueryRunStatus.SUCCEEDED,
        "failed": PostgresQueryRunStatus.FAILED,
        "cancelled": PostgresQueryRunStatus.CANCELLED,
    }[result.status]
    async with session_factory() as session:
        try:
            update_result = await session.execute(
                update(PostgresQueryRun)
                .where(PostgresQueryRun.id == result.execution_id)
                .values(
                    status=status,
                    completed_at=result.completed_at,
                    failure_code=result.failure_code,
                    result_truncated=(result.safety_row_truncated or result.safety_byte_truncated),
                    result_complete=result.complete,
                    returned_row_count=result.returned_row_count,
                )
            )
            if update_result.rowcount != 1:
                raise _ExecutionBoundaryError("execution_recording_failed")
            await session.commit()
        except _ExecutionBoundaryError:
            await _rollback_quietly(session)
            raise
        except SQLAlchemyError:
            await _rollback_quietly(session)
            raise _ExecutionBoundaryError("execution_recording_failed") from None


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass


async def _verify_metric_execution_pin(verifier: MetricExecutionPinVerifier) -> None:
    """Require a fresh trusted Stage 5A pin without importing metric service.

    The callback is deliberately evaluated at the two gateway boundaries that
    matter: before a run can persist a semantic version UUID, and immediately
    before any remote PostgreSQL work.  A failed, malformed, or unavailable
    verifier is always equivalent to a changed metric.
    """

    try:
        if not await verifier():
            raise _ExecutionBoundaryError("metric_definition_changed")
    except _ExecutionBoundaryError:
        raise
    except Exception:
        raise _ExecutionBoundaryError("metric_definition_changed") from None


async def _verify_relationship_execution_pin(verifier: RelationshipExecutionPinVerifier) -> None:
    """Require a fresh relationship lifecycle/catalog pin at the gateway.

    The callback is intentionally independent from the compiler's resolver:
    both must still agree immediately before remote work. A timeout, service
    error, malformed callback, retirement, or catalog incompatibility fails
    closed and never becomes a permissive fallback.
    """

    try:
        if not await verifier():
            raise _ExecutionBoundaryError("relationship_definition_changed")
    except _ExecutionBoundaryError:
        raise
    except Exception:
        raise _ExecutionBoundaryError("relationship_definition_changed") from None


async def _verify_metric_version_source(
    session_factory: _SessionFactory,
    *,
    source_id: UUID,
    metric_definition_version_id: UUID,
) -> None:
    """Check the optional provenance UUID belongs to this source before write.

    The caller-supplied verifier establishes lifecycle/current-version intent;
    this independent lookup prevents a buggy internal caller from persisting a
    real metric version UUID from a different source even if its callback is
    incorrectly permissive.  The database composite FK remains the durable
    race-safe backstop.
    """

    async with session_factory() as session:
        try:
            exists = (
                await session.execute(
                    select(MetricDefinitionVersion.id).where(
                        MetricDefinitionVersion.id == metric_definition_version_id,
                        MetricDefinitionVersion.source_id == source_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise _ExecutionBoundaryError("metric_definition_changed")
        except _ExecutionBoundaryError:
            raise
        except SQLAlchemyError:
            raise _ExecutionBoundaryError("metric_definition_changed") from None
        finally:
            await _rollback_quietly(session)


async def _verify_relationship_version_source(
    session_factory: _SessionFactory,
    *,
    source_id: UUID,
    relationship_definition_version_id: UUID,
) -> None:
    """Require the persisted relationship version to belong to this source.

    The trusted callback proves that the relationship remains current and
    usable. This independent, source-scoped query prevents an accidentally
    permissive internal callback from attaching a real version UUID from
    another source to execution provenance. The database composite foreign
    key is the durable race-safe backstop once the run is inserted.
    """

    async with session_factory() as session:
        try:
            exists = (
                await session.execute(
                    select(RelationshipDefinitionVersion.id).where(
                        RelationshipDefinitionVersion.id == relationship_definition_version_id,
                        RelationshipDefinitionVersion.source_id == source_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise _ExecutionBoundaryError("relationship_definition_changed")
        except _ExecutionBoundaryError:
            raise
        except SQLAlchemyError:
            raise _ExecutionBoundaryError("relationship_definition_changed") from None
        finally:
            await _rollback_quietly(session)


def _semantics(compiled: CompiledPostgresQuery) -> dict[str, Any]:
    return {
        "filters_apply_before_aggregation": compiled.semantics.filters_apply_before_aggregation,
        "post_aggregation_filters_supported": compiled.semantics.post_aggregation_filters_supported,
        "count_all_is_row_count": compiled.semantics.count_all_is_row_count,
        "count_column_excludes_nulls": compiled.semantics.count_column_excludes_nulls,
        "result_order": compiled.semantics.result_order,
        "timestamp_bucket_notes": list(compiled.semantics.timestamp_bucket_notes),
        "join_type": compiled.semantics.join_type,
        "left_join_preserves_unmatched_base_rows": (
            compiled.semantics.left_join_preserves_unmatched_base_rows
        ),
        "lookup_filters_in_on": compiled.semantics.lookup_filters_in_on,
        "lookup_filters_in_where": compiled.semantics.lookup_filters_in_where,
    }


def _relationship_provenance(
    compiled: CompiledPostgresQuery,
) -> PostgresRelationshipProvenance | None:
    if compiled.join is None:
        return None
    return PostgresRelationshipProvenance(
        relationship_id=compiled.join.relationship_id,
        relationship_version_id=compiled.join.relationship_version_id,
        version_number=compiled.join.version_number,
        join_type=compiled.join.join_type,
    )


def _result_columns(
    expected: tuple[ExpectedPostgresOutputColumn, ...],
    actual: tuple[ActualPostgresOutputColumn, ...] = (),
) -> tuple[PostgresQueryResultColumn, ...]:
    actual_by_alias = {column.alias: column for column in actual}
    return tuple(
        PostgresQueryResultColumn(
            alias=column.alias,
            logical_type=column.logical_type,
            expected_postgres_type=column.postgres_type,
            actual_postgres_type=(
                actual_by_alias[column.alias].postgres_type
                if column.alias in actual_by_alias
                else None
            ),
            source_column_id=column.source_column_id,
            source_column_name=column.source_column_name,
            operation=column.operation,
            bucket=column.bucket,
            time_zone=column.time_zone,
        )
        for column in expected
    )


def _ordering(compiled: CompiledPostgresQuery) -> tuple[PostgresQueryOrdering, ...]:
    return tuple(
        PostgresQueryOrdering(alias=item.alias, direction=item.direction) for item in compiled.ordering
    )


# The adapter receives date/timestamp/timestamptz as PostgreSQL's ISO text (DateStyle pinned), so
# ``infinity`` and a real ``9999-12-31`` stay distinct. ``datetime``/``date`` objects are still accepted
# (they mean exactly what they say; there is no object spelling of infinity).
_ISO_DATE = re.compile(r"-?\d{4,7}-\d\d-\d\d(?: BC)?")
_ISO_TIMESTAMP = re.compile(r"\d{4,7}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,6})?(?: BC)?")
_ISO_TIMESTAMPTZ = re.compile(r"\d{4,7}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,6})?(?:[+-]\d\d(?::\d\d)?)?(?: BC)?")
_INFINITIES = frozenset({"infinity", "-infinity"})


def _temporal_text(postgres_type: str, text: str) -> str:
    """Normalise PostgreSQL ISO text to the API spelling; refuse anything not in the pinned ISO form."""
    if text in _INFINITIES:
        return text
    if postgres_type == "date":
        if not _ISO_DATE.fullmatch(text):
            raise _ResultSerializationError()
        return text
    pattern = _ISO_TIMESTAMP if postgres_type == "timestamp" else _ISO_TIMESTAMPTZ
    if not pattern.fullmatch(text):
        raise _ResultSerializationError()
    if text.endswith(" BC") or len(text.split("-")[0]) > 4:
        # Outside Python's datetime range (year 1-9999): returned exactly as PostgreSQL writes it.
        return text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _ResultSerializationError() from None
    if postgres_type == "timestamp":
        return parsed.isoformat()
    if parsed.tzinfo is None:
        raise _ResultSerializationError()
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _serialize_value(value: Any, expected: ExpectedPostgresOutputColumn) -> PostgresResultValue:
    if value is None:
        return PostgresResultValue(kind="null", value=None)
    postgres_type = expected.postgres_type
    if postgres_type in {"int2", "int4", "int8"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _ResultSerializationError()
        # Always use a decimal string: the same format handles JS-safe and
        # unsafe integers without forcing a caller to detect precision loss.
        return PostgresResultValue(kind="integer", value=str(value))
    if postgres_type == "numeric":
        if not isinstance(value, Decimal):
            raise _ResultSerializationError()
        if not value.is_finite():
            # Same spelling as floats below: numeric supports NaN (and Infinity from PostgreSQL 14).
            return PostgresResultValue(
                kind="non_finite_number",
                value="NaN" if value.is_nan() else ("Infinity" if value > 0 else "-Infinity"),
            )
        return PostgresResultValue(kind="decimal", value=format(value, "f"))
    if postgres_type in {"float4", "float8"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _ResultSerializationError()
        number = float(value)
        if math.isfinite(number):
            return PostgresResultValue(kind="number", value=repr(number))
        return PostgresResultValue(
            kind="non_finite_number",
            value="NaN" if math.isnan(number) else ("Infinity" if number > 0 else "-Infinity"),
        )
    if postgres_type in {"text", "varchar", "bpchar"}:
        if not isinstance(value, str):
            raise _ResultSerializationError()
        return PostgresResultValue(kind="text", value=value)
    if postgres_type == "bool":
        if not isinstance(value, bool):
            raise _ResultSerializationError()
        return PostgresResultValue(kind="boolean", value=value)
    if postgres_type == "uuid":
        if not isinstance(value, UUID):
            raise _ResultSerializationError()
        return PostgresResultValue(kind="uuid", value=str(value))
    if postgres_type in {"date", "timestamp", "timestamptz"}:
        if isinstance(value, str):
            return PostgresResultValue(kind=postgres_type, value=_temporal_text(postgres_type, value))
        if postgres_type == "date":
            if isinstance(value, datetime) or not isinstance(value, date):
                raise _ResultSerializationError()
            return PostgresResultValue(kind="date", value=value.isoformat())
        if not isinstance(value, datetime):
            raise _ResultSerializationError()
        if postgres_type == "timestamp":
            if value.tzinfo is not None and value.utcoffset() is not None:
                raise _ResultSerializationError()
            return PostgresResultValue(kind="timestamp", value=value.isoformat())
        if value.tzinfo is None or value.utcoffset() is None:
            raise _ResultSerializationError()
        utc_value = value.astimezone(timezone.utc)
        return PostgresResultValue(kind="timestamptz", value=utc_value.isoformat().replace("+00:00", "Z"))
    raise _ResultSerializationError()


class _RowCollector:
    def __init__(
        self,
        *,
        expected_columns: tuple[ExpectedPostgresOutputColumn, ...],
        byte_limit: int,
    ) -> None:
        self._expected_columns = expected_columns
        self._byte_limit = byte_limit
        self.rows: list[tuple[PostgresResultValue, ...]] = []
        self.serialized_bytes = 0
        self.byte_truncated = False

    async def consume(self, values: tuple[Any, ...]) -> bool:
        if len(values) != len(self._expected_columns):
            raise _ResultSerializationError()
        serialized = tuple(
            _serialize_value(value, expected)
            for value, expected in zip(values, self._expected_columns, strict=True)
        )
        try:
            payload = json.dumps(
                [value.model_dump(mode="json") for value in serialized],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError):
            raise _ResultSerializationError() from None
        separator_bytes = 1 if self.rows else 0
        if self.serialized_bytes + separator_bytes + len(payload) > self._byte_limit:
            self.byte_truncated = True
            return False
        self.rows.append(serialized)
        self.serialized_bytes += separator_bytes + len(payload)
        return True


def _failure_code(exc: BaseException, *, delivery_phase: bool = False) -> str:
    if isinstance(exc, _ExecutionBoundaryError):
        return exc.code
    if isinstance(exc, PostgresExecutionAdapterError):
        return exc.code
    if isinstance(exc, _ResultSerializationError):
        return "result_shape_mismatch"
    if isinstance(exc, (PostgresQueryPlanValidationError, TypeError, ValueError)):
        return "query_not_authorized" if delivery_phase else "invalid_query_plan"
    if isinstance(exc, RelationshipServiceError):
        # A confirmed relationship may be retired, become incompatible with
        # the current catalog, or otherwise cease to be usable between
        # planning and either gateway boundary. Do not misreport that as a
        # generic transport failure; at delivery it is still an access change
        # and rows remain undisclosable.
        return "access_revoked" if delivery_phase else "relationship_definition_changed"
    if isinstance(exc, (CatalogQueryContextError, NotFoundError)):
        return "access_revoked" if delivery_phase else "query_not_authorized"
    return "execution_failed"


def _failure_result(
    *,
    execution_id: UUID,
    source_id: UUID,
    snapshot_id: UUID,
    compiled: CompiledPostgresQuery | None,
    started_at: datetime,
    started_monotonic: float,
    code: str,
    status: Literal["failed", "cancelled"] = "failed",
) -> PostgresQueryExecutionResult:
    safe_code = code if code in _SAFE_FAILURE_CODES else "execution_failed"
    completed_at = datetime.now(timezone.utc)
    warnings = ("result_not_exposed",) if safe_code in {"access_revoked", "access_changed"} else ()
    return PostgresQueryExecutionResult(
        execution_id=execution_id,
        status=status,
        source_id=source_id,
        snapshot_id=snapshot_id,
        asset_id=compiled.asset_id if compiled is not None else None,
        columns=_result_columns(compiled.expected_output_columns) if compiled is not None else (),
        requested_limit=compiled.effective_limit if compiled is not None else None,
        safety_row_limit=None,
        safety_byte_limit=None,
        complete=False,
        explicit_top_n=compiled.is_explicit_top_n if compiled is not None else False,
        ordering=_ordering(compiled) if compiled is not None else (),
        calculation_semantics=_semantics(compiled) if compiled is not None else {},
        relationship_provenance=(
            _relationship_provenance(compiled) if compiled is not None else None
        ),
        warnings=warnings,
        failure_code=safe_code,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=max(0.0, (monotonic() - started_monotonic) * 1000),
    )


def _success_result(
    *,
    execution_id: UUID,
    compiled: CompiledPostgresQuery,
    remote: RemotePostgresExecution,
    collector: _RowCollector,
    safety_row_limit: int,
    safety_byte_limit: int,
    started_at: datetime,
    started_monotonic: float,
    delivery_binding: PostgresQueryDeliveryBinding,
) -> PostgresQueryExecutionResult:
    byte_truncated = collector.byte_truncated or remote.collector_stopped
    row_truncated = remote.safety_row_truncated
    complete = not row_truncated and not byte_truncated
    warnings: list[str] = []
    if row_truncated:
        warnings.append("safety_row_cap_reached")
    if byte_truncated:
        warnings.append("safety_byte_cap_reached")
    return PostgresQueryExecutionResult(
        execution_id=execution_id,
        status="succeeded",
        source_id=compiled.source_id,
        snapshot_id=compiled.snapshot_id,
        asset_id=compiled.asset_id,
        columns=_result_columns(compiled.expected_output_columns, remote.actual_output_columns),
        rows=tuple(collector.rows),
        returned_row_count=len(collector.rows),
        requested_limit=compiled.effective_limit,
        safety_row_limit=safety_row_limit,
        safety_byte_limit=safety_byte_limit,
        serialized_row_bytes=collector.serialized_bytes,
        safety_row_truncated=row_truncated,
        safety_byte_truncated=byte_truncated,
        complete=complete,
        explicit_top_n=compiled.is_explicit_top_n,
        ordering=_ordering(compiled),
        calculation_semantics=_semantics(compiled),
        relationship_provenance=_relationship_provenance(compiled),
        warnings=tuple(warnings),
        delivery_binding=delivery_binding,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        duration_ms=max(0.0, (monotonic() - started_monotonic) * 1000),
    )


async def _finalize_or_record_failure(
    session_factory: _SessionFactory,
    *,
    result: PostgresQueryExecutionResult,
    run_created: bool,
    started_monotonic: float,
) -> PostgresQueryExecutionResult:
    if not run_created:
        return result
    try:
        await _finalize_run(session_factory, result=result)
        return result
    except _ExecutionBoundaryError:
        # The remote operation already happened.  Do not retry it, expose its
        # rows, or turn the failed bookkeeping into a fresh execution request.
        return _failure_result(
            execution_id=result.execution_id,
            source_id=result.source_id,
            snapshot_id=result.snapshot_id,
            compiled=None,
            started_at=result.started_at,
            started_monotonic=started_monotonic,
            code="execution_recording_failed",
        )


async def execute_postgres_query(
    session_factory: _SessionFactory,
    *,
    owner_user_id: UUID,
    source_id: UUID,
    snapshot_id: UUID,
    plan: PostgresQueryPlan,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
    cancellation: PostgresExecutionCancellation | None = None,
    limiter: PostgresExecutionLimiter | None = None,
    metric_definition_version_id: UUID | None = None,
    metric_pin_verifier: MetricExecutionPinVerifier | None = None,
    relationship_definition_version_id: UUID | None = None,
    relationship_pin_verifier: RelationshipExecutionPinVerifier | None = None,
) -> PostgresQueryExecutionResult:
    """Authorize, execute, and return one bounded typed PostgreSQL result.

    This is the Stage 4C-facing internal API.  It has no HTTP route and never
    accepts raw SQL, a query string, or a previously compiled object.  It does
    not retry remote work after any failure, timeout, cancellation, or
    persistence ambiguity.
    """

    execution_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    started_monotonic = monotonic()
    compiled: CompiledPostgresQuery | None = None
    run_created = False
    delivery_binding: PostgresQueryDeliveryBinding | None = None

    # A semantic version UUID is only trustworthy when a caller carries a
    # fresh owner/catalog pin for that exact definition.  Reject either half
    # of the pair before creating a durable run, so generic callers retain
    # their existing behavior while no unverified UUID can become provenance.
    if (metric_definition_version_id is None) != (metric_pin_verifier is None):
        return _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code="metric_definition_changed",
        )
    if metric_definition_version_id is not None and not isinstance(metric_definition_version_id, UUID):
        return _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code="metric_definition_changed",
        )
    if not isinstance(plan, PostgresQueryPlan):
        return _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code="invalid_query_plan",
        )
    # A two-table statement must carry the exact immutable relationship
    # version it claims to use. Keep generic single-table callers unchanged,
    # but reject half-pairs, non-UUIDs, or a semantic pin without an approved
    # typed join before any app-db provenance can be created.
    if (relationship_definition_version_id is None) != (relationship_pin_verifier is None):
        return _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code="relationship_definition_changed",
        )
    if relationship_definition_version_id is not None and not isinstance(
        relationship_definition_version_id, UUID
    ):
        return _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code="relationship_definition_changed",
        )
    if (plan.join is not None) != (relationship_definition_version_id is not None):
        return _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code="relationship_definition_changed",
        )

    try:
        # Validate once before creating durable provenance, then re-run the
        # same current-head-only authorization after waiting for a slot and
        # immediately before remote connection work.
        compiled, _initial_expectation = await _authorize_and_resolve(
            session_factory,
            owner_user_id=owner_user_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            plan=plan,
            settings=settings,
        )
        if relationship_pin_verifier is not None:
            assert relationship_definition_version_id is not None
            if (
                compiled.join is None
                or compiled.join.relationship_version_id != relationship_definition_version_id
            ):
                raise _ExecutionBoundaryError("relationship_definition_changed")
            await _verify_relationship_execution_pin(relationship_pin_verifier)
            await _verify_relationship_version_source(
                session_factory,
                source_id=source_id,
                relationship_definition_version_id=relationship_definition_version_id,
            )
        if metric_pin_verifier is not None:
            # Verify before `_create_run`: a caller cannot attach an arbitrary
            # or foreign version UUID to durable query provenance.
            await _verify_metric_execution_pin(metric_pin_verifier)
            assert metric_definition_version_id is not None
            await _verify_metric_version_source(
                session_factory,
                source_id=source_id,
                metric_definition_version_id=metric_definition_version_id,
            )
        await _create_run(
            session_factory,
            execution_id=execution_id,
            owner_user_id=owner_user_id,
            compiled=compiled,
            started_at=started_at,
            metric_definition_version_id=metric_definition_version_id,
            relationship_definition_version_id=relationship_definition_version_id,
        )
        run_created = True
        effective_limiter = limiter or _limiter_for(settings)
        async with effective_limiter.slot(owner_user_id=owner_user_id, source_id=source_id):
            if cancellation is not None and cancellation.cancelled:
                raise PostgresExecutionCancelledError()
            compiled, expectation = await _authorize_and_resolve(
                session_factory,
                owner_user_id=owner_user_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                plan=plan,
                settings=settings,
            )
            source = await _load_execution_source(
                session_factory,
                owner_user_id=owner_user_id,
                source_id=source_id,
                expectation=expectation,
                settings=settings,
            )
            # Retain only non-secret source authorization facts so a typed
            # remote failure can still be delivered safely through chat.  A
            # later configuration/scope change invalidates this binding there.
            delivery_binding = PostgresQueryDeliveryBinding(
                owner_user_id=owner_user_id,
                source_id=getattr(source, "source_id", source_id),
                snapshot_id=compiled.snapshot_id,
                configuration_generation=getattr(source, "configuration_generation", 0),
                required_relations=tuple(
                    sorted(
                        (relation.schema_name, relation.table_name)
                        for relation in expectation.relation_expectations()
                    )
                ),
            )
            if metric_pin_verifier is not None:
                # This is intentionally the last authorization action before
                # the client gateway receives a remote execution request.
                await _verify_metric_execution_pin(metric_pin_verifier)
            if relationship_pin_verifier is not None:
                # Compile/authorize above also resolves the exact current
                # version and both scoped assets. This last callback closes
                # the lifecycle race immediately before gateway I/O.
                if (
                    compiled.join is None
                    or compiled.join.relationship_version_id
                    != relationship_definition_version_id
                ):
                    raise _ExecutionBoundaryError("relationship_definition_changed")
                await _verify_relationship_execution_pin(relationship_pin_verifier)
            # The compiled statement already retains the caller's requested
            # LIMIT.  Keep the independent execution safety cap separate in
            # both enforcement and result metadata, so a requested top-N is
            # never mislabeled as safety truncation.
            safety_row_limit = settings.postgres_execution_max_result_rows
            collector = _RowCollector(
                expected_columns=compiled.expected_output_columns,
                byte_limit=settings.postgres_execution_max_result_bytes,
            )
            factory = adapter_factory or _build_adapter
            remote = await factory(settings).execute(
                host=source.host,
                port=source.port,
                database_name=source.database_name,
                username=source.username,
                password=source.password,
                custom_ca_pem=source.custom_ca_pem,
                compiled=compiled,
                expectation=expectation,
                max_rows=safety_row_limit,
                row_consumer=collector.consume,
                cancellation=cancellation,
            )
            if cancellation is not None and cancellation.cancelled:
                raise PostgresExecutionCancelledError()

            # A remote success is not permission to disclose rows.  Recheck
            # ownership/current head/scope after the client transaction closes.
            delivered_compiled, delivery_expectation = await _authorize_and_resolve(
                session_factory,
                owner_user_id=owner_user_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                plan=plan,
                settings=settings,
            )
            await _verify_delivery_source_access(
                session_factory,
                owner_user_id=owner_user_id,
                source_id=source_id,
                expectation=delivery_expectation,
                # Internal callers/tests that construct a legacy execution
                # source still receive the existing delivery checks. Real
                # sources always carry the persisted generation fence.
                expected_configuration_generation=getattr(source, "configuration_generation", 0),
                settings=settings,
            )
            if metric_pin_verifier is not None:
                # Keep a completed remote operation from becoming a semantic
                # result after its confirmed metric has changed. The chat
                # layer performs the same guard before persistence/SSE; this
                # gateway check protects all internal callers.
                await _verify_metric_execution_pin(metric_pin_verifier)
                assert metric_definition_version_id is not None
                await _verify_metric_version_source(
                    session_factory,
                    source_id=source_id,
                    metric_definition_version_id=metric_definition_version_id,
                )
            if relationship_pin_verifier is not None:
                # A successful remote read is never itself permission to
                # disclose rows. Resolve the exact immutable relationship
                # again through the caller's fresh-session pin callback and
                # independently retain its source-local provenance boundary.
                if (
                    delivered_compiled.join is None
                    or delivered_compiled.join.relationship_version_id
                    != relationship_definition_version_id
                ):
                    raise _ExecutionBoundaryError("relationship_definition_changed")
                await _verify_relationship_execution_pin(relationship_pin_verifier)
                assert relationship_definition_version_id is not None
                await _verify_relationship_version_source(
                    session_factory,
                    source_id=source_id,
                    relationship_definition_version_id=relationship_definition_version_id,
                )
            if cancellation is not None and cancellation.cancelled:
                raise PostgresExecutionCancelledError()
            if delivered_compiled.fingerprint != compiled.fingerprint:
                raise _ExecutionBoundaryError("access_revoked")
            delivery_binding = PostgresQueryDeliveryBinding(
                owner_user_id=owner_user_id,
                source_id=getattr(source, "source_id", source_id),
                snapshot_id=compiled.snapshot_id,
                configuration_generation=getattr(source, "configuration_generation", 0),
                required_relations=tuple(
                    sorted(
                        (relation.schema_name, relation.table_name)
                        for relation in delivery_expectation.relation_expectations()
                    )
                ),
            )
            result = _success_result(
                execution_id=execution_id,
                compiled=compiled,
                remote=remote,
                collector=collector,
                safety_row_limit=safety_row_limit,
                safety_byte_limit=settings.postgres_execution_max_result_bytes,
                started_at=started_at,
                started_monotonic=started_monotonic,
                delivery_binding=delivery_binding,
            )
    except asyncio.CancelledError:
        cancelled = _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=compiled,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code="execution_cancelled",
            status="cancelled",
        )
        if run_created:
            try:
                await asyncio.shield(_finalize_run(session_factory, result=cancelled))
            except Exception:
                pass
        raise
    except PostgresExecutionCancelledError as exc:
        result = _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=compiled,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code=_failure_code(exc),
            status="cancelled",
        )
    except BaseException as exc:
        # Expected connector, catalog, and serialization failures return a
        # safe typed result.  Programming/system exits retain normal behavior.
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        result = _failure_result(
            execution_id=execution_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            compiled=compiled,
            started_at=started_at,
            started_monotonic=started_monotonic,
            code=_failure_code(exc, delivery_phase=run_created),
        )
    if delivery_binding is not None and result.delivery_binding is None:
        result = result.model_copy(update={"delivery_binding": delivery_binding})
    return await _finalize_or_record_failure(
        session_factory,
        result=result,
        run_created=run_created,
        started_monotonic=started_monotonic,
    )


__all__ = [
    "PostgresExecutionCancellation",
    "PostgresExecutionLimiter",
    "MetricExecutionPinVerifier",
    "RelationshipExecutionPinVerifier",
    "PostgresMetricProvenance",
    "PostgresRelationshipProvenance",
    "PostgresQueryDeliveryBinding",
    "PostgresQueryExecutionResult",
    "PostgresQueryOrdering",
    "PostgresQueryResultColumn",
    "PostgresResultValue",
    "execute_postgres_query",
    "postgres_query_delivery_guard",
]
