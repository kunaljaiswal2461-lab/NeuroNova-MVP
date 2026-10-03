"""Restricted aggregate-only PostgreSQL profiling transport.

This adapter is intentionally separate from the public analytical
``PostgresQueryPlan`` language.  It accepts only typed, catalog-derived
relation and column metadata, builds a small fixed allowlist of aggregate
operations itself, and never returns sampled rows or SQL text.  A caller must
provide the durable run/ownership/snapshot fencing around this short-lived
remote operation.

The one initial sampling strategy is PostgreSQL ``TABLESAMPLE SYSTEM`` with a
bound repeatable seed and a bounded ``LIMIT`` inside a materialized CTE.  All
column measures in a request are computed from that one CTE, so they share the
same observed population.  SYSTEM sampling is block based, and therefore is
not described here as a representative random sample.  Timeout, lock, column,
and operation limits remain necessary because a row limit alone does not bound
database work.
"""
from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

import asyncpg

from app.connectors.connection_policy import ConnectionPolicyError, PostgresConnectionPolicy
from app.connectors.postgres_query_executor import (
    LiveColumnExpectation,
    LiveRelationExpectation,
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
    _LIVE_COLUMNS_SQL,
    _LIVE_RELATION_SQL,
    _SESSION_ASSERTIONS_SQL,
    _SESSION_SEARCH_PATH_SQL,
    _SESSION_TIME_ZONE_SQL,
    _close_quietly,
    _quote_identifier,
    _session_timeout_sql,
    _terminate_quietly,
    _validate_live_columns,
    _validate_restricted_relation,
    _validate_session_assumptions,
)
from app.core.config import Settings


# Keep the transport's hard ceilings independent of configuration.  A future
# setting may narrow these values but cannot silently turn a profiling request
# into a full-table read by raising an unbounded setting.
_MAX_PROFILE_COLUMNS = 50
_MAX_PROFILE_SAMPLE_ROWS = 10_000
_MAX_PROFILE_SAMPLE_PERCENT = 10.0
_MAX_PROFILE_OPERATION_TIMEOUT_SECONDS = 120.0
_MAX_IDENTIFIER_BYTES = 63

_SCALAR_NATIVE_TYPES = frozenset(
    {
        "int2",
        "int4",
        "int8",
        "numeric",
        "float4",
        "float8",
        "bool",
        "uuid",
        "date",
        "timestamp",
        "timestamptz",
        "text",
        "varchar",
        "bpchar",
    }
)
_NUMERIC_NATIVE_TYPES = frozenset({"int2", "int4", "int8", "numeric", "float4", "float8"})


class PostgresProfilingRequestError(PostgresExecutionAdapterError):
    """The typed profiling input is outside the fixed internal contract."""

    code = "profiling_request_invalid"


class ProfileOperation(str, Enum):
    """The complete Stage 6A remote-operation allowlist.

    These are operation *kinds*, not SQL fragments.  The adapter owns every
    expression used to implement them.
    """

    ROW_COUNT = "ROW_COUNT"
    COLUMN_NULL_DISTINCT = "COLUMN_NULL_DISTINCT"
    NUMERIC_SUMMARY = "NUMERIC_SUMMARY"


class ProfileColumnStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"


@dataclass(frozen=True)
class ProfiledRelation:
    """One current catalog-derived ordinary PostgreSQL table identity."""

    schema_name: str
    table_name: str

    def __post_init__(self) -> None:
        _validate_identifier(self.schema_name)
        _validate_identifier(self.table_name)


@dataclass(frozen=True)
class ProfiledColumn:
    """One current catalog-derived column; it is never a client value."""

    column_id: uuid.UUID
    column_name: str
    native_type_schema: str | None
    native_type_name: str | None
    native_type_oid: int | None
    native_type_modifier: int | None
    is_nullable: bool

    def __post_init__(self) -> None:
        _validate_profiled_column(self)

    @property
    def is_supported_scalar(self) -> bool:
        return (
            self.native_type_schema == "pg_catalog"
            and self.native_type_name in _SCALAR_NATIVE_TYPES
        )

    @property
    def is_supported_numeric(self) -> bool:
        return (
            self.native_type_schema == "pg_catalog"
            and self.native_type_name in _NUMERIC_NATIVE_TYPES
        )


@dataclass(frozen=True)
class ProfileSamplingConfig:
    """The fixed, explicitly labelled SYSTEM-table-sample configuration."""

    method: Literal["table_sample_system"] = "table_sample_system"
    # A database ``NUMERIC`` may arrive as ``Decimal`` when a durable run is
    # reloaded, so accept it at this typed boundary and normalize only for the
    # bound remote parameter/result metadata.
    sample_percent: float | Decimal = 1.0
    sample_limit: int = 1_000
    repeatable_seed: int = 1

    def __post_init__(self) -> None:
        if self.method != "table_sample_system":
            raise PostgresProfilingRequestError()
        if (
            isinstance(self.sample_percent, bool)
            or not isinstance(self.sample_percent, (int, float, Decimal))
            or not math.isfinite(float(self.sample_percent))
            or not 0.0 < float(self.sample_percent) <= _MAX_PROFILE_SAMPLE_PERCENT
        ):
            raise PostgresProfilingRequestError()
        if (
            isinstance(self.sample_limit, bool)
            or not isinstance(self.sample_limit, int)
            or not 1 <= self.sample_limit <= _MAX_PROFILE_SAMPLE_ROWS
        ):
            raise PostgresProfilingRequestError()
        if (
            isinstance(self.repeatable_seed, bool)
            or not isinstance(self.repeatable_seed, int)
            or not 0 <= self.repeatable_seed <= 2_147_483_647
        ):
            raise PostgresProfilingRequestError()


@dataclass(frozen=True)
class ProfileExecutionBudget:
    """Bounded remote-session timings supplied by the durable worker."""

    connect_timeout_seconds: float
    statement_timeout_seconds: float
    lock_timeout_seconds: float
    idle_transaction_timeout_seconds: float
    operation_timeout_seconds: float

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.connect_timeout_seconds, 30.0),
            (self.statement_timeout_seconds, 30.0),
            (self.lock_timeout_seconds, 30.0),
            (self.idle_transaction_timeout_seconds, 60.0),
            (self.operation_timeout_seconds, _MAX_PROFILE_OPERATION_TIMEOUT_SECONDS),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 < float(value) <= maximum
            ):
                raise PostgresProfilingRequestError()

    @classmethod
    def from_query_execution_defaults(cls, settings: Settings) -> "ProfileExecutionBudget":
        """Use current conservative connector timings until Stage 6A config narrows them.

        The worker may instead construct this value from Stage 6A-specific
        settings.  Keeping the budget explicit makes that policy visible at
        the call boundary and avoids a hidden fallback to unconstrained reads.
        """

        return cls(
            connect_timeout_seconds=settings.postgres_execution_connect_timeout_seconds,
            statement_timeout_seconds=settings.postgres_execution_statement_timeout_seconds,
            lock_timeout_seconds=settings.postgres_execution_lock_timeout_seconds,
            idle_transaction_timeout_seconds=settings.postgres_execution_idle_transaction_timeout_seconds,
            operation_timeout_seconds=settings.postgres_execution_operation_timeout_seconds,
        )


@dataclass(frozen=True)
class ProfileRequest:
    """A typed aggregate-only request for one current catalog table."""

    relation: ProfiledRelation
    columns: tuple[ProfiledColumn, ...]
    sampling: ProfileSamplingConfig
    operations: tuple[ProfileOperation, ...]
    budget: ProfileExecutionBudget

    def __post_init__(self) -> None:
        _validate_profile_request(self)


@dataclass(frozen=True)
class ProfileNumericSummary:
    """Numeric aggregate values only; no sampled value is retained."""

    observed_non_null_count: int
    sum_value: Decimal | None
    minimum_value: Decimal | None
    maximum_value: Decimal | None
    mean_value: Decimal | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ProfileColumnObservation:
    """One column's precise aggregate observation for the shared sample."""

    column_id: uuid.UUID
    column_name: str
    status: ProfileColumnStatus
    warning_codes: tuple[str, ...]
    observed_non_null_count: int | None
    observed_null_count: int | None
    observed_distinct_count: int | None
    numeric_summary: ProfileNumericSummary | None


@dataclass(frozen=True)
class ProfileExecutionResult:
    """Aggregate-only output suitable for durable observation persistence."""

    collected_at: datetime
    sampling_method: Literal["table_sample_system"]
    sample_percent: float
    sample_limit: int
    repeatable_seed: int
    observed_row_count: int
    measure_coverage: Literal["exact_for_observed_sample"]
    columns: tuple[ProfileColumnObservation, ...]


@dataclass(frozen=True)
class _CompiledProfileRequest:
    """Private statement details; SQL never crosses the adapter boundary."""

    sql: str
    parameters: tuple[float | int, ...]
    supported_columns: tuple[ProfiledColumn, ...]


ConnectFn = Callable[..., Awaitable[Any]]


def _validate_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
    ):
        raise PostgresProfilingRequestError()
    return value


def _validate_profiled_column(column: ProfiledColumn) -> None:
    if not isinstance(column.column_id, uuid.UUID):
        raise PostgresProfilingRequestError()
    _validate_identifier(column.column_name)
    if column.native_type_schema is not None:
        _validate_identifier(column.native_type_schema)
    if column.native_type_name is not None:
        _validate_identifier(column.native_type_name)
    for value in (column.native_type_oid, column.native_type_modifier):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise PostgresProfilingRequestError()
    if not isinstance(column.is_nullable, bool):
        raise PostgresProfilingRequestError()


def _validate_profile_request(request: ProfileRequest) -> None:
    if not isinstance(request.relation, ProfiledRelation):
        raise PostgresProfilingRequestError()
    _validate_identifier(request.relation.schema_name)
    _validate_identifier(request.relation.table_name)
    if not isinstance(request.sampling, ProfileSamplingConfig) or not isinstance(
        request.budget, ProfileExecutionBudget
    ):
        raise PostgresProfilingRequestError()
    # Recheck the frozen dataclasses at the remote boundary as an internal
    # caller can bypass ``__post_init__`` with object-level construction.
    ProfileSamplingConfig(
        method=request.sampling.method,
        sample_percent=request.sampling.sample_percent,
        sample_limit=request.sampling.sample_limit,
        repeatable_seed=request.sampling.repeatable_seed,
    )
    ProfileExecutionBudget(
        connect_timeout_seconds=request.budget.connect_timeout_seconds,
        statement_timeout_seconds=request.budget.statement_timeout_seconds,
        lock_timeout_seconds=request.budget.lock_timeout_seconds,
        idle_transaction_timeout_seconds=request.budget.idle_transaction_timeout_seconds,
        operation_timeout_seconds=request.budget.operation_timeout_seconds,
    )
    if (
        not isinstance(request.columns, tuple)
        or len(request.columns) > _MAX_PROFILE_COLUMNS
        or not isinstance(request.operations, tuple)
        or not request.operations
        or ProfileOperation.ROW_COUNT not in request.operations
        or len(set(request.operations)) != len(request.operations)
    ):
        raise PostgresProfilingRequestError()
    column_ids: set[uuid.UUID] = set()
    column_names: set[str] = set()
    for column in request.columns:
        if not isinstance(column, ProfiledColumn):
            raise PostgresProfilingRequestError()
        _validate_profiled_column(column)
        if column.column_id in column_ids or column.column_name in column_names:
            raise PostgresProfilingRequestError()
        column_ids.add(column.column_id)
        column_names.add(column.column_name)
    if any(not isinstance(operation, ProfileOperation) for operation in request.operations):
        raise PostgresProfilingRequestError()


def _supported_columns(request: ProfileRequest) -> tuple[ProfiledColumn, ...]:
    return tuple(column for column in request.columns if column.is_supported_scalar)


def _quote_catalog_identifier(identifier: str) -> str:
    # ``_quote_identifier`` is shared with the restricted query transport and
    # validates again before escaping.  The input has already been checked as
    # catalog-shaped above; this second boundary is intentional.
    return _quote_identifier(_validate_identifier(identifier))


def _build_profile_request(request: ProfileRequest) -> _CompiledProfileRequest:
    """Compile only the fixed aggregate allowlist over one shared sample CTE."""

    _validate_profile_request(request)
    supported_columns = _supported_columns(request)
    operation_set = set(request.operations)
    projected_columns = (
        ", ".join(_quote_catalog_identifier(column.column_name) for column in supported_columns)
        if supported_columns
        else "1 AS __nv_profile_sample_marker"
    )
    select_items = ["COUNT(*)::bigint AS __nv_profile_observed_rows"]
    for index, column in enumerate(supported_columns):
        identifier = _quote_catalog_identifier(column.column_name)
        if ProfileOperation.COLUMN_NULL_DISTINCT in operation_set:
            select_items.extend(
                (
                    f"COUNT({identifier})::bigint AS __nv_profile_non_null_{index}",
                    f"COUNT(DISTINCT {identifier})::bigint AS __nv_profile_distinct_{index}",
                )
            )
        if ProfileOperation.NUMERIC_SUMMARY in operation_set and column.is_supported_numeric:
            # Text conversion happens only after the server has aggregated a
            # numeric column.  It avoids retaining an arbitrary asyncpg value
            # shape while the parser below rejects non-finite results.
            select_items.extend(
                (
                    f"COUNT({identifier})::bigint AS __nv_profile_numeric_count_{index}",
                    f"SUM({identifier})::text AS __nv_profile_sum_{index}",
                    f"MIN({identifier})::text AS __nv_profile_min_{index}",
                    f"MAX({identifier})::text AS __nv_profile_max_{index}",
                    f"AVG({identifier})::text AS __nv_profile_mean_{index}",
                )
            )
    relation = (
        f"{_quote_catalog_identifier(request.relation.schema_name)}."
        f"{_quote_catalog_identifier(request.relation.table_name)}"
    )
    sql = (
        "WITH __nv_profile_observed AS MATERIALIZED (\n"
        f"    SELECT {projected_columns}\n"
        f"    FROM {relation} TABLESAMPLE SYSTEM ($1::real) REPEATABLE ($2::double precision)\n"
        "    LIMIT $3::bigint\n"
        ")\n"
        "SELECT "
        + ", ".join(select_items)
        + "\nFROM __nv_profile_observed"
    )
    return _CompiledProfileRequest(
        sql=sql,
        parameters=(
            float(request.sampling.sample_percent),
            float(request.sampling.repeatable_seed),
            request.sampling.sample_limit,
        ),
        supported_columns=supported_columns,
    )


def _live_relation_expectation(
    request: ProfileRequest,
    supported_columns: tuple[ProfiledColumn, ...],
) -> LiveRelationExpectation:
    return LiveRelationExpectation(
        schema_name=request.relation.schema_name,
        table_name=request.relation.table_name,
        columns=tuple(
            LiveColumnExpectation(
                name=column.column_name,
                native_type_schema=column.native_type_schema,
                native_type_name=column.native_type_name,
                native_type_oid=column.native_type_oid,
                native_type_modifier=column.native_type_modifier,
                is_nullable=column.is_nullable,
            )
            for column in supported_columns
        ),
    )


def _row_value(row: Any, alias: str) -> Any:
    if row is None:
        raise PostgresExecutionSchemaChangedError()
    try:
        return row[alias]
    except (KeyError, TypeError, IndexError):
        raise PostgresExecutionSchemaChangedError() from None


def _nonnegative_count(row: Any, alias: str) -> int:
    value = _row_value(row, alias)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PostgresExecutionSchemaChangedError()
    return value


def _finite_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    # The query casts numeric aggregates to text.  Accepting an arbitrary
    # driver object here would make the persistence contract less predictable.
    if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
        raise PostgresExecutionSchemaChangedError()
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _numeric_summary_from_row(row: Any, index: int) -> ProfileNumericSummary:
    count = _nonnegative_count(row, f"__nv_profile_numeric_count_{index}")
    values = tuple(
        _finite_decimal(_row_value(row, f"__nv_profile_{kind}_{index}"))
        for kind in ("sum", "min", "max", "mean")
    )
    if count == 0:
        if any(value is not None for value in values):
            raise PostgresExecutionSchemaChangedError()
        return ProfileNumericSummary(
            observed_non_null_count=0,
            sum_value=None,
            minimum_value=None,
            maximum_value=None,
            mean_value=None,
        )
    if any(value is None for value in values):
        # Do not preserve a backend's NaN/Infinity spelling or a malformed
        # aggregate.  The caller can persist this as unavailable instead of
        # accidentally rendering it as a valid numeric observation.
        return ProfileNumericSummary(
            observed_non_null_count=count,
            sum_value=None,
            minimum_value=None,
            maximum_value=None,
            mean_value=None,
            unavailable_reason="non_finite_numeric_result",
        )
    return ProfileNumericSummary(
        observed_non_null_count=count,
        sum_value=values[0],
        minimum_value=values[1],
        maximum_value=values[2],
        mean_value=values[3],
    )


def _aggregate_result(
    request: ProfileRequest,
    compiled: _CompiledProfileRequest,
    row: Any,
) -> ProfileExecutionResult:
    observed_row_count = _nonnegative_count(row, "__nv_profile_observed_rows")
    supported_indexes = {column.column_id: index for index, column in enumerate(compiled.supported_columns)}
    operation_set = set(request.operations)
    observations: list[ProfileColumnObservation] = []
    for column in request.columns:
        index = supported_indexes.get(column.column_id)
        if index is None:
            observations.append(
                ProfileColumnObservation(
                    column_id=column.column_id,
                    column_name=column.column_name,
                    status=ProfileColumnStatus.UNSUPPORTED_TYPE,
                    warning_codes=("unsupported_scalar_type",),
                    observed_non_null_count=None,
                    observed_null_count=None,
                    observed_distinct_count=None,
                    numeric_summary=None,
                )
            )
            continue
        non_null_count: int | None = None
        null_count: int | None = None
        distinct_count: int | None = None
        if ProfileOperation.COLUMN_NULL_DISTINCT in operation_set:
            non_null_count = _nonnegative_count(row, f"__nv_profile_non_null_{index}")
            distinct_count = _nonnegative_count(row, f"__nv_profile_distinct_{index}")
            if non_null_count > observed_row_count or distinct_count > non_null_count:
                raise PostgresExecutionSchemaChangedError()
            null_count = observed_row_count - non_null_count
        numeric_summary = (
            _numeric_summary_from_row(row, index)
            if ProfileOperation.NUMERIC_SUMMARY in operation_set and column.is_supported_numeric
            else None
        )
        if numeric_summary is not None and numeric_summary.observed_non_null_count > observed_row_count:
            raise PostgresExecutionSchemaChangedError()
        warnings: tuple[str, ...] = (
            (numeric_summary.unavailable_reason,)
            if numeric_summary is not None and numeric_summary.unavailable_reason is not None
            else ()
        )
        observations.append(
            ProfileColumnObservation(
                column_id=column.column_id,
                column_name=column.column_name,
                status=ProfileColumnStatus.SUPPORTED,
                warning_codes=warnings,
                observed_non_null_count=non_null_count,
                observed_null_count=null_count,
                observed_distinct_count=distinct_count,
                numeric_summary=numeric_summary,
            )
        )
    return ProfileExecutionResult(
        collected_at=datetime.now(timezone.utc),
        sampling_method=request.sampling.method,
        sample_percent=float(request.sampling.sample_percent),
        sample_limit=request.sampling.sample_limit,
        repeatable_seed=request.sampling.repeatable_seed,
        observed_row_count=observed_row_count,
        measure_coverage="exact_for_observed_sample",
        columns=tuple(observations),
    )


class PostgresProfilingAdapter:
    """Execute one fixed aggregate-only profile against a restricted source."""

    def __init__(
        self,
        *,
        settings: Settings,
        policy: PostgresConnectionPolicy | None = None,
        connect: ConnectFn = asyncpg.connect,
    ) -> None:
        self._settings = settings
        self._policy = policy or PostgresConnectionPolicy()
        self._connect = connect

    async def execute(
        self,
        *,
        host: str,
        port: int,
        database_name: str,
        username: str,
        password: str,
        custom_ca_pem: str | None,
        request: ProfileRequest,
        cancellation: PostgresExecutionCancellation | None = None,
    ) -> ProfileExecutionResult:
        """Return only fixed aggregate observations over one bounded sample.

        This method intentionally does not accept SQL, a query plan, a
        relation name separate from ``request``, result callbacks, or a retry
        directive.  A failed/ambiguous run is for the durable worker to record,
        never for this adapter to repeat automatically.
        """

        _validate_profile_request(request)
        compiled = _build_profile_request(request)
        expectation = _live_relation_expectation(request, compiled.supported_columns)
        if cancellation is not None and cancellation.cancelled:
            raise PostgresExecutionCancelledError()

        budget = request.budget
        connection: Any | None = None
        abort_connection = False
        try:
            async with asyncio.timeout(budget.operation_timeout_seconds):
                normalized_host = await self._policy.validate(host=host, port=port)
                ssl_context = self._policy.build_ssl_context(custom_ca_pem)
                connection = await self._connect(
                    host=normalized_host,
                    port=port,
                    database=database_name,
                    user=username,
                    password=password,
                    ssl=ssl_context,
                    timeout=budget.connect_timeout_seconds,
                    command_timeout=budget.statement_timeout_seconds,
                    server_settings={
                        "application_name": "neuronova-postgres-profiling",
                        "default_transaction_read_only": "on",
                        "search_path": "pg_catalog",
                        "TimeZone": "UTC",
                        "statement_timeout": str(int(budget.statement_timeout_seconds * 1000)),
                        "lock_timeout": str(int(budget.lock_timeout_seconds * 1000)),
                        "idle_in_transaction_session_timeout": str(
                            int(budget.idle_transaction_timeout_seconds * 1000)
                        ),
                    },
                )
                if cancellation is not None:
                    await cancellation.bind(connection)
                async with connection.transaction(readonly=True, isolation="read_committed"):
                    await connection.execute(_SESSION_SEARCH_PATH_SQL)
                    await connection.execute(_SESSION_TIME_ZONE_SQL)
                    await connection.execute(
                        _session_timeout_sql(
                            "statement_timeout", int(budget.statement_timeout_seconds * 1000)
                        )
                    )
                    await connection.execute(
                        _session_timeout_sql("lock_timeout", int(budget.lock_timeout_seconds * 1000))
                    )
                    await connection.execute(
                        _session_timeout_sql(
                            "idle_in_transaction_session_timeout",
                            int(budget.idle_transaction_timeout_seconds * 1000),
                        )
                    )
                    _validate_session_assumptions(
                        await connection.fetchrow(_SESSION_ASSERTIONS_SQL)
                    )
                    before = await connection.fetchrow(
                        _LIVE_RELATION_SQL,
                        expectation.schema_name,
                        expectation.table_name,
                    )
                    if before is None:
                        raise PostgresExecutionSchemaChangedError()
                    relation_oid = _validate_restricted_relation(before, expectation)
                    lock_sql = (
                        "LOCK TABLE "
                        f"{_quote_catalog_identifier(expectation.schema_name)}."
                        f"{_quote_catalog_identifier(expectation.table_name)} IN ACCESS SHARE MODE"
                    )
                    await connection.execute(lock_sql)
                    after = await connection.fetchrow(
                        _LIVE_RELATION_SQL,
                        expectation.schema_name,
                        expectation.table_name,
                    )
                    if after is None or _validate_restricted_relation(after, expectation) != relation_oid:
                        raise PostgresExecutionSchemaChangedError()
                    live_columns = (
                        await connection.fetch(
                            _LIVE_COLUMNS_SQL,
                            relation_oid,
                            [column.name for column in expectation.columns],
                        )
                        if expectation.columns
                        else ()
                    )
                    _validate_live_columns(live_columns, expectation)
                    if cancellation is not None and cancellation.cancelled:
                        abort_connection = True
                        raise PostgresExecutionCancelledError()
                    aggregate_row = await connection.fetchrow(compiled.sql, *compiled.parameters)
                    if cancellation is not None and cancellation.cancelled:
                        abort_connection = True
                        raise PostgresExecutionCancelledError()
                    return _aggregate_result(request, compiled, aggregate_row)
        except asyncio.CancelledError:
            abort_connection = True
            raise
        except PostgresExecutionAdapterError:
            abort_connection = True
            raise
        except TimeoutError:
            abort_connection = True
            raise PostgresExecutionTimeoutError() from None
        except ConnectionPolicyError:
            raise PostgresExecutionConfigurationError() from None
        except Exception as exc:
            abort_connection = True
            if cancellation is not None and cancellation.cancelled:
                raise PostgresExecutionCancelledError() from None
            if isinstance(exc, asyncpg.InsufficientPrivilegeError):
                raise PostgresExecutionAccessChangedError() from None
            if isinstance(
                exc,
                (
                    asyncpg.InvalidPasswordError,
                    asyncpg.InvalidAuthorizationSpecificationError,
                ),
            ):
                raise PostgresExecutionCredentialError() from None
            if isinstance(
                exc,
                (
                    asyncpg.UndefinedTableError,
                    asyncpg.UndefinedColumnError,
                    asyncpg.WrongObjectTypeError,
                    asyncpg.InvalidColumnReferenceError,
                ),
            ):
                raise PostgresExecutionSchemaChangedError() from None
            if isinstance(exc, (asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError)):
                raise PostgresExecutionTimeoutError() from None
            if exc.__class__.__module__.startswith("ssl"):
                raise PostgresExecutionTLSFailureError() from None
            # Never preserve a driver string: it can contain endpoint,
            # credential-role, SQL, or remote value details.
            raise PostgresExecutionAdapterError() from None
        finally:
            if cancellation is not None and connection is not None:
                await cancellation.unbind(connection)
            if connection is not None:
                if abort_connection or (cancellation is not None and cancellation.cancelled):
                    await _terminate_quietly(connection)
                else:
                    await _close_quietly(connection)


__all__ = [
    "ProfileColumnObservation",
    "ProfileColumnStatus",
    "ProfileExecutionBudget",
    "ProfileExecutionResult",
    "ProfileNumericSummary",
    "ProfileOperation",
    "ProfileRequest",
    "ProfileSamplingConfig",
    "ProfiledColumn",
    "ProfiledRelation",
    "PostgresProfilingAdapter",
    "PostgresProfilingRequestError",
]
