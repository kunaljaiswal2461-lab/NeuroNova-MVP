"""Read example values of text columns from a restricted source (SPEC-07 B2).

One short-lived connection per table, opened through ``PostgresQueryExecutionAdapter._run_in_session`` so the
Part 2 guards apply unchanged: read-only transaction, ``search_path = pg_catalog``, the ``postgres_execution_*``
timeouts, ``client_connection_check_interval``, the restricted-role / RLS / partition / inheritance checks, an
ACCESS SHARE lock taken by catalog identity and re-checked, and the live column check.

Two paths per table:

* ``pg_stats`` (default): ONE query for all candidate columns of the table. It reads planner statistics; no
  table is scanned.
* exact (only for a column that has no ``pg_stats`` row, and only if the table is at most
  ``sample_values_exact_max_bytes``): ``GROUP BY`` over that column, behind the EXPLAIN cost gate and the
  per-row byte guard. A larger table without statistics is reported and never scanned.

The adapter returns raw numbers and strings; every decision (cardinality, coverage, personal data, truncation)
is made by the pure functions in ``app.services.sample_values.select``. It never returns SQL text or driver
messages, and a failure is a ``PostgresExecutionAdapterError`` subclass carrying only a fixed code.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.connectors.postgres_query_executor import (
    LiveColumnExpectation,
    LiveRelationExpectation,
    PostgresExecutionConfigurationError,
    PostgresExecutionCostExceededError,
    PostgresExecutionCancellation,
    PostgresQueryExecutionAdapter,
    _plan_total_cost,
    _quote_identifier,
    _wrap_row_size_guard,
)

_RELATION_SIZE_SQL = """
SELECT classes.reltuples::float8 AS reltuples,
       pg_catalog.pg_relation_size(classes.oid)::bigint AS relation_bytes
FROM pg_catalog.pg_class AS classes
WHERE classes.oid = $1::oid
"""

_PG_STATS_SQL = """
SELECT attname, n_distinct, null_frac,
       most_common_vals::text::text[] AS vals, most_common_freqs AS freqs
FROM pg_catalog.pg_stats
WHERE schemaname = $1 AND tablename = $2 AND NOT inherited AND attname = ANY($3::text[])
"""


@dataclass(frozen=True)
class SampleColumnSpec:
    """One catalog-derived text column; never a client value."""

    name: str
    native_type_schema: str | None
    native_type_name: str | None
    native_type_oid: int | None
    native_type_modifier: int | None
    is_nullable: bool


@dataclass(frozen=True)
class SampleTableRequest:
    schema_name: str
    table_name: str
    columns: tuple[SampleColumnSpec, ...]


@dataclass(frozen=True)
class RawColumnStats:
    n_distinct: float | None
    null_frac: float | None
    vals: tuple[str | None, ...] | None
    freqs: tuple[float, ...] | None


@dataclass(frozen=True)
class SampleTableResult:
    reltuples: float | None
    relation_bytes: int
    stats: dict[str, RawColumnStats] = field(default_factory=dict)
    # column -> rows of (value or None, count), at most max_distinct + 1 groups (the LIMIT)
    exact: dict[str, tuple[tuple[str | None, int], ...]] = field(default_factory=dict)
    no_statistics: tuple[str, ...] = ()


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def exact_sql(schema_name: str, table_name: str, column_name: str, limit: int) -> str:
    """The one scanning statement. Every identifier is catalog-derived and quoted; ``limit`` is an app integer."""
    column = _quote_identifier(column_name)
    table = f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
    return (
        f"SELECT {column}::text AS v, count(*) AS n FROM {table} "
        f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(limit)}"
    )


class PostgresSampleAdapter(PostgresQueryExecutionAdapter):
    """Collect planner statistics (and, for small unanalysed tables, exact groups) for text columns."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Application-owned statement texts, in execution order, for tests (I4). Contains identifiers, never values.
        self.statements: list[str] = []

    async def read_table(
        self,
        *,
        host: str,
        port: int,
        database_name: str,
        username: str,
        password: str,
        custom_ca_pem: str | None,
        request: SampleTableRequest,
        max_distinct: int,
        exact_max_bytes: int,
        cancellation: PostgresExecutionCancellation | None = None,
    ) -> SampleTableResult:
        if not request.columns or max_distinct < 1 or exact_max_bytes < 0:
            raise PostgresExecutionConfigurationError()
        expectation = LiveRelationExpectation(
            schema_name=request.schema_name,
            table_name=request.table_name,
            columns=tuple(
                LiveColumnExpectation(
                    name=c.name,
                    native_type_schema=c.native_type_schema,
                    native_type_name=c.native_type_name,
                    native_type_oid=c.native_type_oid,
                    native_type_modifier=c.native_type_modifier,
                    is_nullable=c.is_nullable,
                )
                for c in request.columns
            ),
        )
        names = [c.name for c in request.columns]

        async def body(connection: Any) -> SampleTableResult:
            oids = await self._verify_and_lock_relations(connection, (expectation,))
            oid = oids[(request.schema_name, request.table_name)]
            self.statements.append(_RELATION_SIZE_SQL)
            size_row = await connection.fetchrow(_RELATION_SIZE_SQL, oid)
            if size_row is None:
                raise PostgresExecutionConfigurationError()
            reltuples = _finite_or_none(size_row["reltuples"])
            relation_bytes = int(size_row["relation_bytes"])

            self.statements.append(_PG_STATS_SQL)
            rows = await connection.fetch(_PG_STATS_SQL, request.schema_name, request.table_name, names)
            stats: dict[str, RawColumnStats] = {}
            for row in rows:
                vals = row["vals"]
                freqs = row["freqs"]
                stats[row["attname"]] = RawColumnStats(
                    n_distinct=_finite_or_none(row["n_distinct"]),
                    null_frac=_finite_or_none(row["null_frac"]),
                    vals=tuple(vals) if vals is not None else None,
                    freqs=tuple(float(f) for f in freqs) if freqs is not None else None,
                )

            exact: dict[str, tuple[tuple[str | None, int], ...]] = {}
            no_statistics: list[str] = []
            for column in names:
                if column in stats:
                    continue
                if relation_bytes > exact_max_bytes:
                    no_statistics.append(column)        # I4: never a big scan
                    continue
                exact[column] = await self._exact_groups(
                    connection, request.schema_name, request.table_name, column, max_distinct + 1
                )
            return SampleTableResult(
                reltuples=reltuples,
                relation_bytes=relation_bytes,
                stats=stats,
                exact=exact,
                no_statistics=tuple(no_statistics),
            )

        return await self._run_in_session(
            host=host,
            port=port,
            database_name=database_name,
            username=username,
            password=password,
            custom_ca_pem=custom_ca_pem,
            cancellation=cancellation,
            body=body,
        )

    async def _exact_groups(
        self, connection: Any, schema_name: str, table_name: str, column: str, limit: int
    ) -> tuple[tuple[str | None, int], ...]:
        sql = _wrap_row_size_guard(exact_sql(schema_name, table_name, column, limit), self._settings.postgres_sql_max_row_bytes)
        plan_cost = _plan_total_cost(await connection.fetchval(f"EXPLAIN (FORMAT JSON) {sql}"))
        if plan_cost > self._settings.postgres_sql_max_plan_cost:
            raise PostgresExecutionCostExceededError()
        self.statements.append(sql)
        records = await connection.fetch(sql)
        return tuple((r["v"], int(r["n"])) for r in records)


__all__ = [
    "PostgresSampleAdapter",
    "RawColumnStats",
    "SampleColumnSpec",
    "SampleTableRequest",
    "SampleTableResult",
    "exact_sql",
]
