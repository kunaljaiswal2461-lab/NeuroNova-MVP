"""Builders for pass-2 tests: typed results, privacy flags and a stub that answers with a scripted spec."""
from __future__ import annotations

import uuid
from typing import Any

from app.schemas.nl_sql_record import NlSqlTurn  # noqa: F401  (import check)
from app.services.column_privacy import PrivacyFlags
from app.services.postgres_query_executor import PostgresResultValue
from app.services.sql_execution import SqlExecutionResult, SqlResultColumn
from app.services.nl_sql.pass2_input import prepare_pass2
from app.services.nl_sql.pass2_prompt import build_user2
from app.services.nl_sql.types import Envelope, Encodings, OutputIntent

SOURCE = uuid.uuid4()


def T(v: str | None) -> PostgresResultValue:
    return PostgresResultValue(kind="null", value=None) if v is None else PostgresResultValue(kind="text", value=v)


def I(v: int | None) -> PostgresResultValue:
    return PostgresResultValue(kind="null", value=None) if v is None else PostgresResultValue(kind="integer", value=str(v))


def D(v: str | None) -> PostgresResultValue:
    return PostgresResultValue(kind="null", value=None) if v is None else PostgresResultValue(kind="decimal", value=v)


def F(v: str) -> PostgresResultValue:
    return PostgresResultValue(kind="non_finite_number" if v in ("NaN", "Infinity", "-Infinity") else "number", value=v)


def TS(v: str, kind: str = "timestamptz") -> PostgresResultValue:
    return PostgresResultValue(kind=kind, value=v)


def B(v: bool) -> PostgresResultValue:
    return PostgresResultValue(kind="boolean", value=v)


def result(columns: list[tuple[str, str]], rows: list[list[Any]], sql: str = "SELECT 1 FROM sales.orders", **flags: Any):
    return SqlExecutionResult(
        run_id=uuid.uuid4(), status="succeeded", sql=sql,
        columns=tuple(SqlResultColumn(label=l, pg_type=t) for l, t in columns),
        rows=tuple(tuple(r) for r in rows), row_count=len(rows), source_id=SOURCE, **flags,
    )


def envelope(intent_type: str | None = "bar", assumptions: tuple[str, ...] = (), **enc: Any) -> Envelope:
    encodings = Encodings(x=enc.get("x"), y=enc.get("y", []), series=enc.get("series"), label=enc.get("label"), value=enc.get("value"))
    return Envelope(
        kind="sql", sql="SELECT 1", assumptions=list(assumptions), tables_used=[],
        output_intent=OutputIntent(type=intent_type, encodings=encodings) if intent_type else None,
        clarification=None, reason_code=None, reason=None,
    )


def flags(personal: set[tuple[str, str, str]] = frozenset()) -> PrivacyFlags:
    return PrivacyFlags({k: True for k in personal}, {})


def prepared(execution, *, consent: bool = True, personal_columns: set[tuple[str, str, str]] = frozenset(),
             question: str = "q", env: Envelope | None = None, max_rows: int = 50, budget: int = 6000):
    env = env or envelope()
    return prepare_pass2(
        execution, flags=flags(personal_columns), consent=consent, max_rows=max_rows, budget_tokens=budget,
        measure=lambda inp: build_user2(question=question, assumptions=env.assumptions, intent=env.output_intent, inp=inp),
    )
