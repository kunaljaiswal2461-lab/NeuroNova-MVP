"""Turn a ``SqlExecutionResult`` into the ``PostgresQueryExecutionResult`` the chat transcript and the table/chart
already understand (SPEC-09 W2.3), so the existing persistence normaliser and the frontend table work unchanged.
"""
from __future__ import annotations

from datetime import datetime

from app.services.postgres_query_executor import PostgresQueryExecutionResult, PostgresQueryResultColumn
from app.services.sql_execution import SqlExecutionResult

from .display import kind_of


def to_chat_result(
    execution: SqlExecutionResult,
    *,
    started_at: datetime,
    completed_at: datetime,
    row_cap: int,
) -> PostgresQueryExecutionResult:
    """``execution.applied_limit`` is the validated LIMIT: the model's own when it is within ``row_cap``."""
    truncated = execution.safety_row_truncated or execution.byte_truncated
    limit = execution.applied_limit
    own_limit = limit if limit is not None and 1 <= limit <= row_cap else None
    return PostgresQueryExecutionResult(
        execution_id=execution.run_id,
        status=execution.status,
        source_id=execution.source_id,
        snapshot_id=execution.snapshot_id,
        asset_id=None,
        columns=tuple(
            PostgresQueryResultColumn(
                alias=c.label,
                logical_type=kind_of(c.pg_type) or c.pg_type,
                expected_postgres_type=c.pg_type,
                actual_postgres_type=c.pg_type,
            )
            for c in execution.columns
        ),
        rows=execution.rows,
        returned_row_count=execution.row_count,
        requested_limit=own_limit,
        safety_row_truncated=execution.safety_row_truncated,
        safety_byte_truncated=execution.byte_truncated,
        complete=execution.status == "succeeded" and not truncated,
        explicit_top_n=own_limit is not None,
        ordering=(),
        calculation_semantics={"route": "sql"},
        session_time_zone="UTC",
        failure_code=execution.failure_code,
        delivery_binding=execution.delivery_binding,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=execution.elapsed_ms,
    )
