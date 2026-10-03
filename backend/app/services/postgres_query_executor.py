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
import uuid
from collections.abc import AsyncIterator, Callable
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
    LiveQueryExpectation,
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
from app.db.models.catalog import CatalogColumn
from app.db.models.postgres_query_run import PostgresQueryRun, PostgresQueryRunStatus
from app.db.models.source import DataSource, SourceStatus, SourceType
from app.exceptions.custom_exceptions import NotFoundError
from app.services.catalog_service import CatalogQueryContextError, resolve_postgres_query_catalog_context_for_owner
from app.services.postgres_query_compiler import (
    CompiledPostgresQuery,
    ExpectedPostgresOutputColumn,
    PostgresQueryPlanValidationError,
    validate_and_compile_postgres_query,
)
from app.services.postgres_query_plan import PostgresQueryPlan
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
    session_time_zone: str = "UTC"
    warnings: tuple[str, ...] = ()
    failure_code: str | None = None
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
        }


class _SessionFactory(Protocol):
    def __call__(self) -> Any: ...


class _ExecutionBoundaryError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ResultSerializationError(Exception):
    pass


@dataclass(frozen=True)
class _ExecutionSource:
    source_id: UUID
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


def _referenced_column_ids(plan: PostgresQueryPlan) -> tuple[UUID, ...]:
    references: list[UUID] = []
    references.extend(item.column_id for item in plan.projections)
    references.extend(item.column_id for item in plan.filters)
    references.extend(item.column_id for item in plan.group_by)
    references.extend(item.column_id for item in plan.aggregations if item.column_id is not None)
    return tuple(dict.fromkeys(references))


def _runtime_expectation(
    *,
    compiled: CompiledPostgresQuery,
    asset: Any,
    columns: dict[UUID, CatalogColumn],
) -> LiveQueryExpectation:
    if asset.id != compiled.asset_id:
        raise _ExecutionBoundaryError("catalog_unavailable")
    return LiveQueryExpectation(
        schema_name=asset.schema_name,
        table_name=asset.table_name,
        columns=tuple(
            LiveColumnExpectation(
                name=column.column_name,
                native_type_schema=column.native_type_schema,
                native_type_name=column.native_type_name,
                native_type_oid=column.native_type_oid,
                native_type_modifier=column.native_type_modifier,
                is_nullable=column.is_nullable,
            )
            for column in sorted(columns.values(), key=lambda value: (value.ordinal_position, value.id))
        ),
        expected_output_columns=compiled.expected_output_columns,
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
            context = await resolve_postgres_query_catalog_context_for_owner(
                session,
                owner_user_id=owner_user_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                asset_id=plan.asset_id,
                column_ids=_referenced_column_ids(plan),
            )
            expectation = _runtime_expectation(
                compiled=compiled,
                asset=context.asset,
                columns=dict(context.columns),
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
    expectation: LiveQueryExpectation,
    settings: Settings,
) -> _ExecutionSource:
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
            if (
                source is None
                or source.source_type is not SourceType.POSTGRESQL
                or source.status is not SourceStatus.TESTED
                or source.credential is None
                or (expectation.schema_name, expectation.table_name)
                not in {(scope.schema_name, scope.table_name) for scope in source.table_scopes}
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
            if (
                source is None
                or source.source_type is not SourceType.POSTGRESQL
                or source.status is not SourceStatus.TESTED
                or source.credential is None
                or (expectation.schema_name, expectation.table_name)
                not in {(scope.schema_name, scope.table_name) for scope in source.table_scopes}
            ):
                raise _ExecutionBoundaryError("access_revoked")
        except _ExecutionBoundaryError:
            raise
        except SQLAlchemyError:
            raise _ExecutionBoundaryError("catalog_unavailable") from None
        finally:
            await _rollback_quietly(session)


async def _create_run(
    session_factory: _SessionFactory,
    *,
    execution_id: UUID,
    owner_user_id: UUID,
    compiled: CompiledPostgresQuery,
    started_at: datetime,
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


def _semantics(compiled: CompiledPostgresQuery) -> dict[str, Any]:
    return {
        "filters_apply_before_aggregation": compiled.semantics.filters_apply_before_aggregation,
        "post_aggregation_filters_supported": compiled.semantics.post_aggregation_filters_supported,
        "count_all_is_row_count": compiled.semantics.count_all_is_row_count,
        "count_column_excludes_nulls": compiled.semantics.count_column_excludes_nulls,
        "result_order": compiled.semantics.result_order,
        "timestamp_bucket_notes": list(compiled.semantics.timestamp_bucket_notes),
    }


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
        if not isinstance(value, Decimal) or not value.is_finite():
            raise _ResultSerializationError()
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
    if postgres_type == "date":
        if isinstance(value, datetime) or not isinstance(value, date):
            raise _ResultSerializationError()
        return PostgresResultValue(kind="date", value=value.isoformat())
    if postgres_type == "timestamp":
        if not isinstance(value, datetime) or (value.tzinfo is not None and value.utcoffset() is not None):
            raise _ResultSerializationError()
        return PostgresResultValue(kind="timestamp", value=value.isoformat())
    if postgres_type == "timestamptz":
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
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
        warnings=tuple(warnings),
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
        await _create_run(
            session_factory,
            execution_id=execution_id,
            owner_user_id=owner_user_id,
            compiled=compiled,
            started_at=started_at,
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
                settings=settings,
            )
            if cancellation is not None and cancellation.cancelled:
                raise PostgresExecutionCancelledError()
            if delivered_compiled.fingerprint != compiled.fingerprint:
                raise _ExecutionBoundaryError("access_revoked")
            result = _success_result(
                execution_id=execution_id,
                compiled=compiled,
                remote=remote,
                collector=collector,
                safety_row_limit=safety_row_limit,
                safety_byte_limit=settings.postgres_execution_max_result_bytes,
                started_at=started_at,
                started_monotonic=started_monotonic,
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
    return await _finalize_or_record_failure(
        session_factory,
        result=result,
        run_created=run_created,
        started_monotonic=started_monotonic,
    )


__all__ = [
    "PostgresExecutionCancellation",
    "PostgresExecutionLimiter",
    "PostgresQueryExecutionResult",
    "PostgresQueryOrdering",
    "PostgresQueryResultColumn",
    "PostgresResultValue",
    "execute_postgres_query",
]
