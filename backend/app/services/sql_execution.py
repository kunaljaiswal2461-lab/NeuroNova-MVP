"""Execute model SQL that the validator has accepted (SPEC-05, Part 2).

``execute_validated_sql`` is the only door: it authorizes the caller, validates the text against the
source's *current* catalog head, seals the result, and hands the sealed query to the restricted adapter.
The text that reaches the database is the validator's regenerated SQL, never the caller's string.
No SQL text and no literal value is written to the application database; the SQL travels only in the
returned result (Part 7 stores it in the chat message).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.connectors.postgres_query_executor import (
    ActualPostgresOutputColumn,
    LiveRelationExpectation,
    LiveSqlExpectation,
    PostgresExecutionAdapterError,
    PostgresExecutionCancellation,
    PostgresExecutionCancelledError,
    PostgresExecutionCostExceededError,
    PostgresExecutionUnsupportedOutputError,
)
from app.core.config import Settings
from app.db.models.catalog import CatalogColumn
from app.db.models.postgres_query_run import (
    PostgresQueryRun,
    PostgresQueryRunAsset,
    PostgresQueryRunStatus,
)
from app.exceptions.custom_exceptions import NotFoundError
from app.services import catalog_service
from app.services.postgres_query_compiler import ExpectedPostgresOutputColumn
from app.services.postgres_query_executor import (
    AdapterFactory,
    PostgresExecutionLimiter,
    PostgresQueryDeliveryBinding,
    PostgresResultValue,
    _ExecutionBoundaryError,
    _ResultSerializationError,
    _RowCollector,
    _SAFE_FAILURE_CODES,
    _SessionFactory,
    _build_adapter,
    _limiter_for,
    _live_columns,
    _load_execution_source,
    _rollback_quietly,
    postgres_query_delivery_guard,
)
from app.services.sql_guard import (
    GuardLimits,
    Rejection,
    SealedValidatedQuery,
    SealError,
    load_catalog_scope,
    seal,
    validate_sql,
)

_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)

# Fixed strings only (I8): nothing here is ever built from database or driver text.
_FAILURE_MESSAGES = {
    "schema_changed": "The data structure changed since the query was checked. Ask again.",
    "access_changed": "Access to the data changed. The query was not run.",
    "access_revoked": "Access to the data changed while the query ran. No rows are shown.",
    "restricted_role_required": "The database role is not restricted enough to run this query.",
    "execution_timeout": "The query took too long and was stopped. Simplify it or add filters.",
    "execution_cancelled": "The query was cancelled.",
    "query_too_expensive": "The query is too expensive to run. Filter earlier, avoid cross joins, "
    "and aggregate before joining large tables.",
    "result_row_too_large": "A result row is too large to return. Select fewer or shorter columns.",
    "query_not_authorized": "The source is not available for this request.",
    "source_unavailable": "The source is not available for this request.",
}
_UNSUPPORTED_OUTPUT_HINTS = {
    "interval": "use extract(epoch from ...) or subtract dates",
    "composite": "select columns, not a whole row",
    "money": "cast to numeric",
    "other": "cast the value to text, numeric or a date type",
}
_DEFAULT_FAILURE_MESSAGE = "The query could not be run."


class SqlResultColumn(BaseModel):
    model_config = _MODEL_CONFIG

    label: str
    pg_type: str


class SqlExecutionResult(BaseModel):
    """A bounded, typed result of one validated-SQL run. Holds no raw database error text."""

    model_config = _MODEL_CONFIG

    run_id: UUID
    status: Literal["succeeded", "failed", "cancelled"]
    sql: str | None = None
    columns: tuple[SqlResultColumn, ...] = ()
    rows: tuple[tuple[PostgresResultValue, ...], ...] = ()
    row_count: int = Field(default=0, ge=0)
    safety_row_truncated: bool = False
    byte_truncated: bool = False
    plan_cost: float | None = None
    elapsed_ms: float = Field(default=0.0, ge=0.0)
    tables: tuple[tuple[str, str], ...] = ()
    failure_code: str | None = None
    message: str | None = None
    source_id: UUID
    snapshot_id: UUID | None = None
    # Implementation-only fence for the chat delivery guard; never serialized.
    delivery_binding: PostgresQueryDeliveryBinding | None = Field(default=None, exclude=True, repr=False)

    @property
    def is_ok(self) -> bool:
        return self.status == "succeeded"


@dataclass(frozen=True)
class _Prepared:
    sealed: SealedValidatedQuery
    expectation: LiveSqlExpectation


def _safe_message(code: str, exc: BaseException | None = None) -> str:
    if isinstance(exc, PostgresExecutionUnsupportedOutputError):
        hint = _UNSUPPORTED_OUTPUT_HINTS.get(exc.hint, _UNSUPPORTED_OUTPUT_HINTS["other"])
        where = f"Output column {int(exc.column_index)}" if exc.column_index >= 1 else "An output column"
        return f"{where} has a type that cannot be returned: {hint}."
    return _FAILURE_MESSAGES.get(code, _DEFAULT_FAILURE_MESSAGE)


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, _ExecutionBoundaryError):
        return exc.code
    if isinstance(exc, PostgresExecutionAdapterError):
        return exc.code
    if isinstance(exc, _ResultSerializationError):
        return "result_shape_mismatch"
    if isinstance(exc, (catalog_service.CatalogQueryContextError, NotFoundError)):
        return "query_not_authorized"
    return "execution_failed"


async def _validate_and_prepare(
    session_factory: _SessionFactory,
    *,
    user_id: UUID,
    source_id: UUID,
    sql: str,
    settings: Settings,
) -> _Prepared | Rejection:
    """Steps 1-3: authorize, validate against the current head, seal, and gather live-check facts."""
    row_cap = settings.postgres_execution_max_result_rows
    async with session_factory() as session:
        try:
            # Owner + active source + current head (the compiler's own resolver).
            context = await catalog_service.get_postgres_chat_catalog_context_for_owner(
                session, source_id=source_id, owner_user_id=user_id
            )
            scope = await load_catalog_scope(
                session, owner_user_id=user_id, source_id=source_id, snapshot_id=context.snapshot.id
            )
            validated = validate_sql(sql, scope, row_cap=row_cap, limits=GuardLimits.from_settings(settings))
            if isinstance(validated, Rejection):
                return validated
            try:
                sealed = seal(validated, scope, row_cap=row_cap)
            except SealError:
                raise _ExecutionBoundaryError("execution_failed") from None
            column_ids = [column.column_id for column in sealed.columns]
            rows = (
                (
                    await session.execute(
                        select(CatalogColumn).where(
                            CatalogColumn.source_id == source_id,
                            CatalogColumn.snapshot_id == sealed.snapshot_id,
                            CatalogColumn.id.in_(column_ids),
                        )
                    )
                ).scalars().all()
                if column_ids
                else []
            )
            by_id = {column.id: column for column in rows}
            if len(by_id) != len(set(column_ids)):
                raise _ExecutionBoundaryError("schema_changed")
            # Read the ORM rows into plain expectations *before* the rollback below expires them.
            relations = tuple(
                LiveRelationExpectation(
                    schema_name=table.schema,
                    table_name=table.name,
                    columns=_live_columns(
                        {
                            column.column_id: by_id[column.column_id]
                            for column in sealed.columns
                            if (column.schema, column.table) == (table.schema, table.name)
                        }
                    ),
                )
                for table in sealed.tables
            )
        except SQLAlchemyError:
            raise _ExecutionBoundaryError("catalog_unavailable") from None
        finally:
            # Never let this read transaction span client-database I/O.
            await _rollback_quietly(session)

    return _Prepared(sealed=sealed, expectation=LiveSqlExpectation(relations=relations))


async def _head_snapshot_id(session_factory: _SessionFactory, *, user_id: UUID, source_id: UUID) -> UUID:
    async with session_factory() as session:
        try:
            context = await catalog_service.get_postgres_chat_catalog_context_for_owner(
                session, source_id=source_id, owner_user_id=user_id
            )
            return context.snapshot.id
        except SQLAlchemyError:
            raise _ExecutionBoundaryError("catalog_unavailable") from None
        finally:
            await _rollback_quietly(session)


async def _create_sql_run(
    session_factory: _SessionFactory,
    *,
    run_id: UUID,
    user_id: UUID,
    sealed: SealedValidatedQuery,
    started_at: datetime,
) -> None:
    async with session_factory() as session:
        try:
            session.add(
                PostgresQueryRun(
                    id=run_id,
                    owner_user_id=user_id,
                    source_id=sealed.source_id,
                    snapshot_id=sealed.snapshot_id,
                    asset_id=None,
                    query_kind="sql",
                    plan_fingerprint=sealed.structural_fingerprint,
                    status=PostgresQueryRunStatus.RUNNING,
                    requested_at=started_at,
                    started_at=started_at,
                    result_truncated=False,
                    result_complete=False,
                    returned_row_count=0,
                )
            )
            await session.flush()
            session.add_all(
                PostgresQueryRunAsset(
                    run_id=run_id,
                    asset_id=table.asset_id,
                    source_id=sealed.source_id,
                    snapshot_id=sealed.snapshot_id,
                )
                for table in sealed.tables
            )
            await session.commit()
        except SQLAlchemyError:
            await _rollback_quietly(session)
            raise _ExecutionBoundaryError("execution_recording_failed") from None


async def _finalize_sql_run(session_factory: _SessionFactory, *, result: SqlExecutionResult) -> None:
    status = {
        "succeeded": PostgresQueryRunStatus.SUCCEEDED,
        "failed": PostgresQueryRunStatus.FAILED,
        "cancelled": PostgresQueryRunStatus.CANCELLED,
    }[result.status]
    async with session_factory() as session:
        try:
            outcome = await session.execute(
                update(PostgresQueryRun)
                .where(PostgresQueryRun.id == result.run_id)
                .values(
                    status=status,
                    completed_at=datetime.now(timezone.utc),
                    failure_code=result.failure_code,
                    result_truncated=result.safety_row_truncated or result.byte_truncated,
                    result_complete=result.is_ok and not (result.safety_row_truncated or result.byte_truncated),
                    returned_row_count=result.row_count,
                )
            )
            if outcome.rowcount != 1:
                raise _ExecutionBoundaryError("execution_recording_failed")
            await session.commit()
        except _ExecutionBoundaryError:
            await _rollback_quietly(session)
            raise
        except SQLAlchemyError:
            await _rollback_quietly(session)
            raise _ExecutionBoundaryError("execution_recording_failed") from None


def _failure(
    *,
    run_id: UUID,
    source_id: UUID,
    snapshot_id: UUID | None,
    started_monotonic: float,
    code: str,
    exc: BaseException | None = None,
    status: Literal["failed", "cancelled"] = "failed",
    sealed: SealedValidatedQuery | None = None,
    plan_cost: float | None = None,
    delivery_binding: PostgresQueryDeliveryBinding | None = None,
) -> SqlExecutionResult:
    safe_code = code if code in _SAFE_FAILURE_CODES else "execution_failed"
    return SqlExecutionResult(
        run_id=run_id,
        status=status,
        # The failed SQL is still the validator's text; a repair step (Part 6) needs it.
        sql=sealed.sql if sealed is not None else None,
        tables=tuple((t.schema, t.name) for t in sealed.tables) if sealed is not None else (),
        plan_cost=plan_cost,
        elapsed_ms=max(0.0, (monotonic() - started_monotonic) * 1000),
        failure_code=safe_code,
        message=_safe_message(safe_code, exc),
        source_id=source_id,
        snapshot_id=snapshot_id,
        delivery_binding=delivery_binding,
    )


async def execute_validated_sql(
    *,
    session_factory: _SessionFactory,
    settings: Settings,
    user_id: UUID,
    source_id: UUID,
    sql: str,
    cancellation: PostgresExecutionCancellation | None = None,
    adapter_factory: AdapterFactory | None = None,
    limiter: PostgresExecutionLimiter | None = None,
) -> SqlExecutionResult | Rejection:
    """Validate, seal, and run one model-written SELECT. See the module docstring and B2 in the spec."""
    run_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    started_monotonic = monotonic()
    sealed: SealedValidatedQuery | None = None
    run_created = False
    plan_cost: float | None = None
    delivery_binding: PostgresQueryDeliveryBinding | None = None

    def failed(code: str, exc: BaseException | None = None, status: Literal["failed", "cancelled"] = "failed"):
        return _failure(
            run_id=run_id,
            source_id=source_id,
            snapshot_id=sealed.snapshot_id if sealed is not None else None,
            started_monotonic=started_monotonic,
            code=code,
            exc=exc,
            status=status,
            sealed=sealed,
            plan_cost=plan_cost,
            delivery_binding=delivery_binding,
        )

    try:
        # 1-3. Authorize, re-validate against the head, seal.
        prepared = await _validate_and_prepare(
            session_factory, user_id=user_id, source_id=source_id, sql=sql, settings=settings
        )
        if isinstance(prepared, Rejection):
            return prepared
        sealed = prepared.sealed
        row_cap = settings.postgres_execution_max_result_rows
        byte_limit = settings.postgres_execution_max_result_bytes

        # 4. Limiter.
        async with (limiter or _limiter_for(settings)).slot(owner_user_id=user_id, source_id=source_id):
            # 5. Run row (structural fingerprint and asset ids only; no SQL text). Created before the
            # cancellation check so a request cancelled while queued is still recorded as CANCELLED.
            await _create_sql_run(
                session_factory, run_id=run_id, user_id=user_id, sealed=sealed, started_at=started_at
            )
            run_created = True
            if cancellation is not None and cancellation.cancelled:
                raise PostgresExecutionCancelledError()

            # The wait for a slot can be long; never run against a snapshot that stopped being the head.
            if await _head_snapshot_id(session_factory, user_id=user_id, source_id=source_id) != sealed.snapshot_id:
                raise _ExecutionBoundaryError("schema_changed")
            source = await _load_execution_source(
                session_factory,
                owner_user_id=user_id,
                source_id=source_id,
                settings=settings,
                required_relations=frozenset((t.schema, t.name) for t in sealed.tables),
            )
            # Non-secret source facts, kept so even a failure can be delivered through chat; a later
            # configuration or scope change invalidates the binding there (same as the compiled path).
            binding = PostgresQueryDeliveryBinding(
                owner_user_id=user_id,
                source_id=source.source_id,
                snapshot_id=sealed.snapshot_id,
                configuration_generation=source.configuration_generation,
                required_relations=tuple(sorted((t.schema, t.name) for t in sealed.tables)),
            )
            delivery_binding = binding

            # 6-7. Adapter path, collecting serialized rows as they stream.
            collector: _RowCollector | None = None

            def on_columns(columns: tuple[ActualPostgresOutputColumn, ...]) -> None:
                nonlocal collector
                collector = _RowCollector(
                    expected_columns=tuple(
                        ExpectedPostgresOutputColumn(
                            alias=column.alias,
                            logical_type=column.postgres_type,
                            postgres_type=column.postgres_type,
                            source_column_id=None,
                            source_column_name=None,
                        )
                        for column in columns
                    ),
                    byte_limit=byte_limit,
                )

            async def consume(values: tuple[Any, ...]) -> bool:
                assert collector is not None
                return await collector.consume(values)

            remote = await (adapter_factory or _build_adapter)(settings).execute_sql(
                host=source.host,
                port=source.port,
                database_name=source.database_name,
                username=source.username,
                password=source.password,
                custom_ca_pem=source.custom_ca_pem,
                sealed=sealed,
                expectation=prepared.expectation,
                max_rows=row_cap,
                row_consumer=consume,
                on_columns=on_columns,
                cancellation=cancellation,
            )
            plan_cost = remote.plan_cost
            assert collector is not None
            if cancellation is not None and cancellation.cancelled:
                raise PostgresExecutionCancelledError()

            # 8. A remote success is not permission to disclose: re-check under the source-row lock.
            async with postgres_query_delivery_guard(
                session_factory, owner_user_id=user_id, binding=binding, settings=settings
            ):
                if cancellation is not None and cancellation.cancelled:
                    raise PostgresExecutionCancelledError()
                result = SqlExecutionResult(
                    run_id=run_id,
                    status="succeeded",
                    sql=sealed.sql,
                    columns=tuple(
                        SqlResultColumn(label=c.alias, pg_type=c.postgres_type)
                        for c in remote.actual_output_columns
                    ),
                    rows=tuple(collector.rows),
                    row_count=len(collector.rows),
                    safety_row_truncated=remote.safety_row_truncated,
                    byte_truncated=collector.byte_truncated or remote.collector_stopped,
                    plan_cost=remote.plan_cost,
                    elapsed_ms=max(0.0, (monotonic() - started_monotonic) * 1000),
                    tables=tuple((t.schema, t.name) for t in sealed.tables),
                    source_id=source_id,
                    snapshot_id=sealed.snapshot_id,
                    delivery_binding=binding,
                )
    except asyncio.CancelledError:
        if run_created:
            try:
                await asyncio.shield(
                    _finalize_sql_run(session_factory, result=failed("execution_cancelled", status="cancelled"))
                )
            except Exception:
                pass
        raise
    except PostgresExecutionCancelledError as exc:
        result = failed(_failure_code(exc), exc, status="cancelled")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        result = failed(_failure_code(exc), exc)

    # 9. Finalize. The remote work already happened: never retry it, never expose rows on a bookkeeping failure.
    if run_created:
        try:
            await _finalize_sql_run(session_factory, result=result)
        except _ExecutionBoundaryError:
            return failed("execution_recording_failed")
    return result


__all__ = [
    "SqlExecutionResult",
    "SqlResultColumn",
    "execute_validated_sql",
]
