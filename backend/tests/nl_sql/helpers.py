"""Scripted pieces for the pipeline tests (SPEC-09 L1): an executor that returns canned results, builders for them."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.services.postgres_query_executor import PostgresResultValue
from app.services.sql_execution import SqlExecutionResult, SqlResultColumn
from app.services.sql_guard import Rejection, RejectionCode
from tests.nl_sql.conftest import GOOD_SQL, envelope

SOURCE = uuid.uuid4()


def int_value(n: int) -> PostgresResultValue:
    return PostgresResultValue(kind="integer", value=str(n))


def ok_result(
    sql: str = "SELECT count(*) AS n FROM sales.orders", n: int = 42, *, source_id=None, snapshot_id=None, **fields: Any
) -> SqlExecutionResult:
    return SqlExecutionResult(
        run_id=uuid.uuid4(), status="succeeded", sql=sql,
        columns=(SqlResultColumn(label="n", pg_type="int8"),), rows=((int_value(n),),), row_count=1,
        source_id=source_id or SOURCE, snapshot_id=snapshot_id, **fields,
    )


def failed_result(code: str, *, error_class: str | None = None, sql: str = "SELECT 1", message: str | None = None):
    return SqlExecutionResult(
        run_id=uuid.uuid4(), status="failed", sql=sql, failure_code=code, error_class=error_class,
        message=message or "The query could not be run.", source_id=SOURCE,
    )


def rejection(code: RejectionCode, identifier: str | None = None, suggestions: tuple[str, ...] = ()) -> Rejection:
    return Rejection(code=code, message=f"Fixed validator message for {code.value}.", identifier=identifier, suggestions=suggestions)


@dataclass
class ScriptedExecutor:
    """Stands in for ``execute_validated_sql``. Each item is a result, a Rejection or an exception."""

    script: list[Any]
    calls: list[dict] = field(default_factory=list)

    async def __call__(self, **kwargs: Any):
        self.calls.append(kwargs)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, Rejection) and (on := kwargs.get("on_validated")) is not None:
            await on()
        return item

    @property
    def sqls(self) -> list[str]:
        return [c["sql"] for c in self.calls]


def sql_env(sql: str, **changes: Any) -> dict:
    return envelope(sql=sql, **changes)


CLARIFY = envelope(
    kind="clarification", sql=None, output_intent=None, tables_used=[], clarification="Which year do you mean?",
)
UNSUPPORTED = envelope(
    kind="unsupported", sql=None, output_intent=None, tables_used=[], reason_code="not_in_schema", reason="No such data.",
)
