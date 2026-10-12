"""Restricted, internal PostgreSQL execution transport for Stage 4B.

This module owns only a short-lived *client database* connection.  It knows
nothing about HTTP, LLMs, application-database sessions, or source
credentials at rest.  Callers supply a compiler-produced statement and facts
resolved from the current authorized catalog; they never supply SQL text.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from app.connectors.connection_policy import ConnectionPolicyError, PostgresConnectionPolicy
from app.core.config import Settings
from app.services.sql_guard.seal import SealedValidatedQuery, is_sealed
from app.services.postgres_query_compiler import (
    CompiledPostgresQuery,
    ExpectedPostgresOutputColumn,
    _is_compiler_minted_postgres_query,
)


class PostgresExecutionAdapterError(Exception):
    """Safe, categorized remote-execution failure; never retain a driver message."""

    code = "execution_failed"


class PostgresExecutionConfigurationError(PostgresExecutionAdapterError):
    code = "unsupported_execution_configuration"


class PostgresExecutionSchemaChangedError(PostgresExecutionAdapterError):
    code = "schema_changed"


class PostgresExecutionAccessChangedError(PostgresExecutionAdapterError):
    code = "access_changed"


class PostgresExecutionCredentialError(PostgresExecutionAdapterError):
    code = "credential_unavailable"


class PostgresExecutionTLSFailureError(PostgresExecutionAdapterError):
    code = "tls_verification_failed"


class PostgresExecutionRestrictedRoleError(PostgresExecutionAdapterError):
    code = "restricted_role_required"


class PostgresExecutionTimeoutError(PostgresExecutionAdapterError):
    code = "execution_timeout"


class PostgresExecutionCancelledError(PostgresExecutionAdapterError):
    code = "execution_cancelled"


class PostgresExecutionUnsupportedOutputError(PostgresExecutionAdapterError):
    """A result column has a type the serializer cannot represent faithfully (SPEC-05 B3.5).

    Carries only the 1-based column position and a hint category; never the database's type text.
    """

    code = "unsupported_output_type"

    def __init__(self, column_index: int = 0, hint: str = "other") -> None:
        super().__init__()
        self.column_index = column_index
        self.hint = hint


class PostgresExecutionCostExceededError(PostgresExecutionAdapterError):
    code = "query_too_expensive"


# SQLSTATEs a model-written query can plausibly cause and that a corrected query can fix (SPEC-09 C5).
# Everything else stays an opaque ``execution_failed``.
REPAIRABLE_SQLSTATES: frozenset[str] = frozenset(
    {"42601", "42703", "42P01", "42883", "42702", "42804", "22P02", "42803", "22012", "22003", "22008"}
)


class PostgresExecutionQueryError(PostgresExecutionAdapterError):
    """The server refused a model-written statement for a reason a rewrite can fix (SPEC-09 W1.9).

    Carries the five-character SQLSTATE only. The server's message, DETAIL, CONTEXT and HINT are never kept.
    """

    code = "query_error"

    def __init__(self, sqlstate: str) -> None:
        super().__init__()
        self.sqlstate = sqlstate


class PostgresExecutionRowTooLargeError(PostgresExecutionAdapterError):
    """One result row exceeded the per-row byte limit; refused by the server before it was sent."""

    code = "result_row_too_large"


def _query_error(exc: BaseException, cancellation: "PostgresExecutionCancellation | None") -> PostgresExecutionQueryError | None:
    """Map a server error from the SQL path to a ``query_error`` when its SQLSTATE is repairable."""
    if cancellation is not None and cancellation.cancelled:
        return None
    if _is_row_too_large(exc):
        return None                       # the size guard reuses 22P02; it has its own code
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate in REPAIRABLE_SQLSTATES:
        return PostgresExecutionQueryError(sqlstate)
    return None


@dataclass(frozen=True)
class LiveColumnExpectation:
    """A catalog-derived input-column fact required by the compiled plan."""

    name: str
    native_type_schema: str | None
    native_type_name: str | None
    native_type_oid: int | None
    native_type_modifier: int | None
    is_nullable: bool


@dataclass(frozen=True)
class LiveUniqueKeyExpectation:
    """A declared target key that must remain live and non-deferrable."""

    constraint_name: str
    key_kind: Literal["primary_key", "unique"]
    column_names: tuple[str, ...]


@dataclass(frozen=True)
class LiveForeignKeyExpectation:
    """A declared base-to-lookup FK that must remain exact while locked.

    Target-key uniqueness prevents fan-out, but it does not prove the
    approved source-side mapping still exists.  Keep both ordered sides here
    so a same-named/replaced constraint or a reordered composite key fails
    before the compiled statement is prepared.
    """

    constraint_name: str
    base_column_names: tuple[str, ...]
    lookup_column_names: tuple[str, ...]


@dataclass(frozen=True)
class LiveRelationExpectation:
    """One compiler-authorized ordinary table involved in a remote read."""

    schema_name: str
    table_name: str
    columns: tuple[LiveColumnExpectation, ...]
    unique_key: LiveUniqueKeyExpectation | None = None


@dataclass(frozen=True)
class LiveQueryExpectation:
    """Trusted structural facts to recheck in the remote transaction."""

    schema_name: str
    table_name: str
    columns: tuple[LiveColumnExpectation, ...]
    expected_output_columns: tuple[ExpectedPostgresOutputColumn, ...]
    # The base relation retains the original Stage 4B fields.  An optional
    # lookup is the only Stage 5B extension: exactly two current same-source
    # ordinary tables, locked in deterministic identity order.
    lookup_relation: LiveRelationExpectation | None = None
    foreign_key: LiveForeignKeyExpectation | None = None

    def relation_expectations(self) -> tuple[LiveRelationExpectation, ...]:
        base = LiveRelationExpectation(
            schema_name=self.schema_name,
            table_name=self.table_name,
            columns=self.columns,
        )
        if self.lookup_relation is None:
            return (base,)
        if (
            self.lookup_relation.schema_name == base.schema_name
            and self.lookup_relation.table_name == base.table_name
        ):
            raise PostgresExecutionSchemaChangedError()
        return (base, self.lookup_relation)


@dataclass(frozen=True)
class ActualPostgresOutputColumn:
    alias: str
    postgres_type: str
    postgres_type_schema: str | None


@dataclass(frozen=True)
class RemotePostgresExecution:
    """Metadata about a streamed execution; values stay with the caller's collector."""

    actual_output_columns: tuple[ActualPostgresOutputColumn, ...]
    safety_row_truncated: bool
    collector_stopped: bool


@dataclass(frozen=True)
class LiveSqlExpectation:
    """Trusted facts for a validated-SQL run: 1..30 ordinary tables and the referenced columns of each."""

    relations: tuple[LiveRelationExpectation, ...]


@dataclass(frozen=True)
class RemoteSqlExecution:
    """Metadata about a streamed validated-SQL execution; values stay with the caller's collector."""

    actual_output_columns: tuple[ActualPostgresOutputColumn, ...]   # labels already de-duplicated
    plan_cost: float
    safety_row_truncated: bool
    collector_stopped: bool


RowConsumer = Callable[[tuple[Any, ...]], bool | Awaitable[bool]]
ConnectFn = Callable[..., Awaitable[Any]]


# The relation is found by bound namespace/name values.  After obtaining an
# ACCESS SHARE lock by catalog-derived quoted identity, the same query is run
# again and its OID must be unchanged.  The lock blocks ordinary incompatible
# DDL until query completion; it cannot prove a pre-lock same-name replacement
# matches a historical catalog snapshot.
_LIVE_RELATION_SQL = """
SELECT classes.oid::bigint AS relation_oid,
       namespaces.nspname AS schema_name,
       classes.relname AS table_name,
       classes.relkind AS relation_kind,
       classes.relispartition AS is_partition,
       classes.relhassubclass AS has_inheritance_children,
       EXISTS (
           SELECT 1
           FROM pg_catalog.pg_inherits AS inheritance
           WHERE inheritance.inhrelid = classes.oid
              OR inheritance.inhparent = classes.oid
       ) AS has_inheritance_membership,
       classes.relrowsecurity AS row_security_enabled,
       classes.relforcerowsecurity AS force_row_security,
       roles.rolsuper AS role_is_superuser,
       roles.rolbypassrls AS role_bypasses_rls,
       classes.relowner = roles.oid AS role_is_table_owner,
       pg_catalog.has_schema_privilege(namespaces.oid, 'USAGE') AS has_schema_usage,
       pg_catalog.has_table_privilege(classes.oid, 'SELECT') AS has_table_select
FROM pg_catalog.pg_class AS classes
JOIN pg_catalog.pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace
JOIN pg_catalog.pg_roles AS roles ON roles.rolname = current_user
WHERE namespaces.nspname = $1::text
  AND classes.relname = $2::text
"""

_LIVE_COLUMNS_SQL = """
SELECT attributes.attname AS column_name,
       type_namespaces.nspname AS native_type_schema,
       types.typname AS native_type_name,
       attributes.atttypid::integer AS native_type_oid,
       attributes.atttypmod AS native_type_modifier,
       NOT attributes.attnotnull AS is_nullable,
       pg_catalog.has_column_privilege($1::oid, attributes.attname, 'SELECT') AS has_column_select
FROM pg_catalog.pg_attribute AS attributes
JOIN pg_catalog.pg_type AS types ON types.oid = attributes.atttypid
LEFT JOIN pg_catalog.pg_namespace AS type_namespaces ON type_namespaces.oid = types.typnamespace
WHERE attributes.attrelid = $1::oid
  AND attributes.attnum > 0
  AND NOT attributes.attisdropped
  AND attributes.attname = ANY($2::text[])
ORDER BY attributes.attnum
"""

# Catalog discovery records only primary/unique constraints, never arbitrary
# indexes. Recheck that exact constraint identity and its ordered key columns
# under the same relation lock. Deferrable, unvalidated, invalid, partial, or
# not-ready evidence is deliberately insufficient for a fan-out-safe join.
_LIVE_UNIQUE_KEY_SQL = """
SELECT constraints.conname AS constraint_name,
       constraints.contype AS constraint_type,
       constraints.condeferrable AS is_deferrable,
       constraints.condeferred AS is_initially_deferred,
       constraints.convalidated AS is_validated,
       indexes.indisunique AS is_unique,
       indexes.indisvalid AS is_valid,
       indexes.indisready AS is_ready,
       indexes.indpred IS NOT NULL AS is_partial,
       key_columns.ordinality AS ordinal_position,
       attributes.attname AS column_name
FROM pg_catalog.pg_constraint AS constraints
JOIN pg_catalog.pg_index AS indexes ON indexes.indexrelid = constraints.conindid
JOIN LATERAL unnest(constraints.conkey) WITH ORDINALITY
  AS key_columns(attnum, ordinality) ON TRUE
JOIN pg_catalog.pg_attribute AS attributes
  ON attributes.attrelid = constraints.conrelid
 AND attributes.attnum = key_columns.attnum
WHERE constraints.conrelid = $1::oid
  AND constraints.conname = $2::text
  AND constraints.contype IN ('p', 'u')
ORDER BY key_columns.ordinality
"""

# Recheck the approved source FK as a declared, ordered mapping after both
# involved relations have been identity-checked and locked.  The base and
# lookup OIDs are bound parameters; no remote names enter this query text.
_LIVE_FOREIGN_KEY_SQL = """
SELECT constraints.conname AS constraint_name,
       constraints.contype AS constraint_type,
       constraints.condeferrable AS is_deferrable,
       constraints.condeferred AS is_initially_deferred,
       constraints.convalidated AS is_validated,
       key_columns.ordinality AS ordinal_position,
       base_attributes.attname AS base_column_name,
       lookup_attributes.attname AS lookup_column_name
FROM pg_catalog.pg_constraint AS constraints
JOIN LATERAL unnest(constraints.conkey, constraints.confkey) WITH ORDINALITY
  AS key_columns(base_attnum, lookup_attnum, ordinality) ON TRUE
JOIN pg_catalog.pg_attribute AS base_attributes
  ON base_attributes.attrelid = constraints.conrelid
 AND base_attributes.attnum = key_columns.base_attnum
JOIN pg_catalog.pg_attribute AS lookup_attributes
  ON lookup_attributes.attrelid = constraints.confrelid
 AND lookup_attributes.attnum = key_columns.lookup_attnum
WHERE constraints.conrelid = $1::oid
  AND constraints.confrelid = $2::oid
  AND constraints.conname = $3::text
  AND constraints.contype = 'f'
ORDER BY key_columns.ordinality
"""

_SESSION_SEARCH_PATH_SQL = "SET LOCAL search_path TO pg_catalog"
_SESSION_TIME_ZONE_SQL = "SET LOCAL TimeZone TO 'UTC'"
_SESSION_CLIENT_CHECK_SQL = (
    "SELECT pg_catalog.set_config('client_connection_check_interval', '250ms', true) "
    "FROM pg_catalog.pg_settings WHERE name = 'client_connection_check_interval'"
)
_SESSION_ASSERTIONS_SQL = """
SELECT current_setting('transaction_read_only') AS transaction_read_only,
       current_setting('search_path') AS search_path,
       current_setting('TimeZone') AS time_zone
"""


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_quietly(connection: Any) -> None:
    try:
        result = connection.close()
        if inspect.isawaitable(result):
            await asyncio.shield(result)
    except Exception:
        # Do not log driver exceptions: they can include endpoint/role detail.
        pass


async def _terminate_quietly(connection: Any) -> None:
    """Stop the dedicated connection rather than merely abandoning its task."""

    try:
        terminate = getattr(connection, "terminate", None)
        if callable(terminate):
            result = terminate()
            if inspect.isawaitable(result):
                await asyncio.shield(result)
    except Exception:
        pass
    await _close_quietly(connection)


class PostgresExecutionCancellation:
    """Internal cancellation handle intended for a later Stage 4C caller.

    It is scoped to one dedicated client connection.  Cancellation terminates
    that connection; it does not mark a query retryable or allow its rows to
    be surfaced later.
    """

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()
        self._connection: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    async def wait_cancelled(self) -> None:
        """Return once ``cancel`` has been requested (lets a model call be dropped with the turn)."""
        await self._cancelled.wait()

    async def bind(self, connection: Any) -> None:
        abort = False
        async with self._lock:
            if self._cancelled.is_set():
                abort = True
            else:
                self._connection = connection
        if abort:
            await _terminate_quietly(connection)
            raise PostgresExecutionCancelledError()

    async def unbind(self, connection: Any) -> None:
        async with self._lock:
            if self._connection is connection:
                self._connection = None

    async def cancel(self) -> None:
        async with self._lock:
            # Taking the same lock as ``delivery_guard`` gives a completed
            # result and an accepted cancellation request one clear ordering.
            # Stage 4C therefore cannot begin storing successful rows after
            # cancellation has already won.
            self._cancelled.set()
            connection = self._connection
        if connection is not None:
            await _terminate_quietly(connection)

    @asynccontextmanager
    async def delivery_guard(self):
        """Serialize a final result write with a cancellation request.

        The guard is used only after the remote connection has returned.
        Holding it while Stage 4C persists a terminal result means a
        cancellation accepted first wins before result rows are stored; a
        cancellation accepted afterwards is ordered after the completed write
        and the caller can still suppress SSE disclosure.
        """

        async with self._lock:
            yield not self._cancelled.is_set()


def _quote_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise PostgresExecutionSchemaChangedError()
    return '"' + identifier.replace('"', '""') + '"'


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        raise PostgresExecutionSchemaChangedError() from None


def _as_bool(row: Any, key: str) -> bool:
    value = _row_value(row, key)
    if not isinstance(value, bool):
        raise PostgresExecutionSchemaChangedError()
    return value


def _relation_oid(row: Any) -> int:
    value = _row_value(row, "relation_oid")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PostgresExecutionSchemaChangedError()
    return value


def _validate_session_assumptions(row: Any) -> None:
    if row is None:
        raise PostgresExecutionConfigurationError()
    if (
        _row_value(row, "transaction_read_only") != "on"
        or str(_row_value(row, "search_path")).strip() != "pg_catalog"
        or str(_row_value(row, "time_zone")).upper() != "UTC"
    ):
        raise PostgresExecutionConfigurationError()


def _session_timeout_sql(setting: str, milliseconds: int) -> str:
    """Return a fixed-format LOCAL setting from bounded application config."""

    if setting not in {
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
    } or milliseconds < 1:
        raise PostgresExecutionConfigurationError()
    return f"SET LOCAL {setting} TO '{milliseconds}ms'"


def _relation_kind(row: Any) -> str:
    """pg_class.relkind is type "char"; asyncpg returns it as bytes (b'r')."""
    value = _row_value(row, "relation_kind")
    return value.decode("ascii") if isinstance(value, bytes) else str(value)


def _validate_restricted_relation(row: Any, expectation: LiveRelationExpectation) -> int:
    oid = _relation_oid(row)
    if (
        _row_value(row, "schema_name") != expectation.schema_name
        or _row_value(row, "table_name") != expectation.table_name
    ):
        raise PostgresExecutionSchemaChangedError()
    # Stage 4B deliberately supports only standalone ordinary tables.  Views,
    # foreign tables, partition roots/leaves, and inheritance may have extra
    # execution/side-effect or schema semantics and need a later contract.
    if (
        _relation_kind(row) != "r"
        or _as_bool(row, "is_partition")
        or _as_bool(row, "has_inheritance_children")
        or _as_bool(row, "has_inheritance_membership")
    ):
        raise PostgresExecutionSchemaChangedError()
    if _as_bool(row, "role_is_superuser") or _as_bool(row, "role_bypasses_rls"):
        raise PostgresExecutionRestrictedRoleError()
    # A table owner can bypass RLS unless FORCE ROW LEVEL SECURITY is in play,
    # and has broader DDL authority than the required read role.  Stage 4B
    # therefore requires a non-owner least-privilege role.  Any RLS table is
    # also outside this initial contract rather than relying on policy code.
    if _as_bool(row, "role_is_table_owner") or _as_bool(row, "row_security_enabled"):
        raise PostgresExecutionRestrictedRoleError()
    if not _as_bool(row, "has_schema_usage") or not _as_bool(row, "has_table_select"):
        raise PostgresExecutionAccessChangedError()
    return oid


def _validate_live_columns(rows: Any, expectation: LiveRelationExpectation) -> None:
    expected = {column.name: column for column in expectation.columns}
    # A pure COUNT(*) plan legitimately references no source columns.  Its
    # relation, role, ACL, prepared-output, and transaction checks still run.
    if not expected:
        if rows:
            raise PostgresExecutionSchemaChangedError()
        return
    if len(rows) != len(expected):
        raise PostgresExecutionSchemaChangedError()
    seen: set[str] = set()
    for row in rows:
        name = _row_value(row, "column_name")
        if not isinstance(name, str) or name in seen:
            raise PostgresExecutionSchemaChangedError()
        seen.add(name)
        wanted = expected.get(name)
        if wanted is None:
            raise PostgresExecutionSchemaChangedError()
        if not _as_bool(row, "has_column_select"):
            raise PostgresExecutionAccessChangedError()
        if (
            _row_value(row, "native_type_schema") != wanted.native_type_schema
            or _row_value(row, "native_type_name") != wanted.native_type_name
            or _row_value(row, "native_type_oid") != wanted.native_type_oid
            or _row_value(row, "native_type_modifier") != wanted.native_type_modifier
            or _row_value(row, "is_nullable") is not wanted.is_nullable
        ):
            raise PostgresExecutionSchemaChangedError()
    if seen != set(expected):
        raise PostgresExecutionSchemaChangedError()


def _validate_live_unique_key(
    rows: Any,
    expectation: LiveUniqueKeyExpectation,
) -> None:
    """Require the exact locked target key used to prevent join fan-out."""

    if not rows:
        raise PostgresExecutionSchemaChangedError()
    expected_constraint_type = "p" if expectation.key_kind == "primary_key" else "u"
    actual_names: list[str] = []
    for expected_position, row in enumerate(rows, start=1):
        if (
            _row_value(row, "constraint_name") != expectation.constraint_name
            or _row_value(row, "constraint_type") != expected_constraint_type
            or _as_bool(row, "is_deferrable")
            or _as_bool(row, "is_initially_deferred")
            or not _as_bool(row, "is_validated")
            or not _as_bool(row, "is_unique")
            or not _as_bool(row, "is_valid")
            or not _as_bool(row, "is_ready")
            or _as_bool(row, "is_partial")
            or _row_value(row, "ordinal_position") != expected_position
        ):
            raise PostgresExecutionSchemaChangedError()
        column_name = _row_value(row, "column_name")
        if not isinstance(column_name, str):
            raise PostgresExecutionSchemaChangedError()
        actual_names.append(column_name)
    if tuple(actual_names) != expectation.column_names:
        raise PostgresExecutionSchemaChangedError()


def _validate_live_foreign_key(
    rows: Any,
    expectation: LiveForeignKeyExpectation,
) -> None:
    """Require the exact, non-deferrable ordered FK approved by the owner."""

    if (
        not expectation.constraint_name
        or not expectation.base_column_names
        or not expectation.lookup_column_names
        or len(expectation.base_column_names) != len(expectation.lookup_column_names)
        or len(rows) != len(expectation.base_column_names)
    ):
        raise PostgresExecutionSchemaChangedError()
    actual_base_names: list[str] = []
    actual_lookup_names: list[str] = []
    for expected_position, row in enumerate(rows, start=1):
        if (
            _row_value(row, "constraint_name") != expectation.constraint_name
            or _row_value(row, "constraint_type") != "f"
            or _as_bool(row, "is_deferrable")
            or _as_bool(row, "is_initially_deferred")
            or not _as_bool(row, "is_validated")
            or _row_value(row, "ordinal_position") != expected_position
        ):
            raise PostgresExecutionSchemaChangedError()
        base_column_name = _row_value(row, "base_column_name")
        lookup_column_name = _row_value(row, "lookup_column_name")
        if not isinstance(base_column_name, str) or not isinstance(lookup_column_name, str):
            raise PostgresExecutionSchemaChangedError()
        actual_base_names.append(base_column_name)
        actual_lookup_names.append(lookup_column_name)
    if (
        tuple(actual_base_names) != expectation.base_column_names
        or tuple(actual_lookup_names) != expectation.lookup_column_names
    ):
        raise PostgresExecutionSchemaChangedError()


def _prepared_attributes(statement: Any, expected: tuple[ExpectedPostgresOutputColumn, ...]) -> tuple[ActualPostgresOutputColumn, ...]:
    attributes_getter = getattr(statement, "get_attributes", None)
    if not callable(attributes_getter):
        raise PostgresExecutionSchemaChangedError()
    attributes = tuple(attributes_getter())
    if len(attributes) != len(expected):
        raise PostgresExecutionSchemaChangedError()
    actual: list[ActualPostgresOutputColumn] = []
    for attribute, wanted in zip(attributes, expected, strict=True):
        name = getattr(attribute, "name", None)
        type_info = getattr(attribute, "type", None)
        type_name = getattr(type_info, "name", None)
        type_schema = getattr(type_info, "schema", None)
        if (
            name != wanted.alias
            or type_name != wanted.postgres_type
            or type_schema not in {None, "pg_catalog"}
        ):
            raise PostgresExecutionSchemaChangedError()
        actual.append(
            ActualPostgresOutputColumn(
                alias=name,
                postgres_type=type_name,
                postgres_type_schema=type_schema,
            )
        )
    return tuple(actual)


def _record_values(record: Any, aliases: tuple[str, ...]) -> tuple[Any, ...]:
    keys_getter = getattr(record, "keys", None)
    if not callable(keys_getter) or tuple(keys_getter()) != aliases:
        raise PostgresExecutionSchemaChangedError()
    try:
        return tuple(record[alias] for alias in aliases)
    except (KeyError, TypeError, IndexError):
        raise PostgresExecutionSchemaChangedError() from None


def _relation_expectations_for_compiled(
    compiled: CompiledPostgresQuery,
    expectation: LiveQueryExpectation,
) -> tuple[LiveRelationExpectation, ...]:
    """Ensure the gateway cannot pair a statement with unrelated live facts."""

    relations = expectation.relation_expectations()
    lookup = expectation.lookup_relation
    if compiled.join is None:
        if lookup is not None or expectation.foreign_key is not None or len(relations) != 1:
            raise PostgresExecutionConfigurationError()
        return relations
    foreign_key = expectation.foreign_key
    if lookup is None or foreign_key is None or len(relations) != 2:
        raise PostgresExecutionSchemaChangedError()
    if (
        lookup.schema_name != compiled.join.lookup_schema_name
        or lookup.table_name != compiled.join.lookup_table_name
        or lookup.unique_key is None
        or lookup.unique_key.constraint_name != compiled.join.target_key_constraint_name
        or lookup.unique_key.key_kind != compiled.join.target_key_kind
        or lookup.unique_key.column_names != compiled.join.target_key_column_names
        or foreign_key.constraint_name != compiled.join.foreign_key_constraint_name
        or foreign_key.base_column_names != compiled.join.foreign_key_base_column_names
        or foreign_key.lookup_column_names != compiled.join.foreign_key_lookup_column_names
        or not foreign_key.base_column_names
        or len(foreign_key.base_column_names) != len(foreign_key.lookup_column_names)
    ):
        raise PostgresExecutionSchemaChangedError()
    lookup_column_names = {column.name for column in lookup.columns}
    if not set(compiled.join.target_key_column_names).issubset(lookup_column_names):
        raise PostgresExecutionSchemaChangedError()
    return relations


# Output types _serialize_value can represent exactly (SPEC-05 F5/B3.5).
SUPPORTED_OUTPUT_TYPES = frozenset(
    {
        "int2", "int4", "int8", "numeric", "float4", "float8",
        "text", "varchar", "bpchar", "bool", "uuid", "date", "timestamp", "timestamptz",
    }
)
MAX_SQL_RELATIONS = 30


def dedupe_output_labels(names: Sequence[str]) -> tuple[str, ...]:
    """Keep Postgres's names in order; ``count, count`` -> ``count, count_2``; empty/``?column?`` -> ``col_<n>``."""
    used: set[str] = set()
    labels: list[str] = []
    for index, raw in enumerate(names, start=1):
        base = raw if isinstance(raw, str) and raw and raw != "?column?" else f"col_{index}"
        label = base
        suffix = 2
        while label in used:
            label = f"{base}_{suffix}"
            suffix += 1
        used.add(label)
        labels.append(label)
    return tuple(labels)


def _unsupported_output_hint(type_name: str, kind: str | None) -> str:
    if type_name == "interval":
        return "interval"
    if type_name == "money":
        return "money"
    if type_name == "record" or kind == "composite":
        return "composite"
    return "other"


def _sql_output_columns(statement: Any) -> tuple[ActualPostgresOutputColumn, ...]:
    """Read prepared attributes and apply the type gate, before any cursor opens."""
    attributes_getter = getattr(statement, "get_attributes", None)
    if not callable(attributes_getter):
        raise PostgresExecutionSchemaChangedError()
    attributes = tuple(attributes_getter())
    if not attributes:
        raise PostgresExecutionConfigurationError()
    labels = dedupe_output_labels([getattr(attribute, "name", "") for attribute in attributes])
    columns: list[ActualPostgresOutputColumn] = []
    for index, (attribute, label) in enumerate(zip(attributes, labels, strict=True), start=1):
        type_info = getattr(attribute, "type", None)
        type_name = getattr(type_info, "name", None)
        type_schema = getattr(type_info, "schema", None)
        if (
            not isinstance(type_name, str)
            or type_name not in SUPPORTED_OUTPUT_TYPES
            or type_schema not in {None, "pg_catalog"}
        ):
            raise PostgresExecutionUnsupportedOutputError(
                index, _unsupported_output_hint(str(type_name), getattr(type_info, "kind", None))
            )
        columns.append(
            ActualPostgresOutputColumn(alias=label, postgres_type=type_name, postgres_type_schema=type_schema)
        )
    return tuple(columns)


_ROW_TOO_LARGE_PREFIX = "nn_row_too_large:"


def _wrap_row_size_guard(sql: str, limit_bytes: int) -> str:
    """Wrap a sealed SELECT so the *server* refuses any row whose text form exceeds ``limit_bytes``.

    The CAST makes the failing branch depend on the row, so the planner cannot fold it away (a constant
    ``CASE`` would be evaluated at plan time and fail every query). ``ROW(q.*)`` names the whole row: a bare
    ``q::text`` resolves to a *column* called ``q`` when the query outputs one, which would bypass the guard. The refusal is SQLSTATE 22P02 with
    ``_ROW_TOO_LARGE_PREFIX``. ``limit_bytes`` is an application integer, never model text.
    """
    limit = int(limit_bytes)
    return (
        f"SELECT q.* FROM ({sql}) AS q WHERE CASE WHEN octet_length(ROW(q.*)::text) > {limit} "
        f"THEN CAST('{_ROW_TOO_LARGE_PREFIX}' || octet_length(ROW(q.*)::text) AS int) = 0 ELSE true END"
    )


def _is_row_too_large(exc: BaseException) -> bool:
    return (
        isinstance(exc, asyncpg.InvalidTextRepresentationError)
        and _ROW_TOO_LARGE_PREFIX in (getattr(exc, "message", None) or str(exc))
    )


async def _install_text_temporal_codecs(connection: Any) -> None:
    """Receive date/timestamp/timestamptz as PostgreSQL's own text (``DateStyle`` is pinned to ISO).

    asyncpg's binary decoding turns ``infinity`` into ``date.max`` etc., which is indistinguishable from a
    real ``9999-12-31``. The text form keeps them apart.
    """
    for type_name in ("date", "timestamp", "timestamptz"):
        await connection.set_type_codec(
            type_name, schema="pg_catalog", encoder=str, decoder=str, format="text"
        )


def _plan_total_cost(explain_json: Any) -> float:
    try:
        payload = json.loads(explain_json) if isinstance(explain_json, (str, bytes)) else explain_json
        cost = payload[0]["Plan"]["Total Cost"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise PostgresExecutionConfigurationError() from None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
        raise PostgresExecutionConfigurationError()
    return float(cost)


class PostgresQueryExecutionAdapter:
    """Open one constrained client connection and stream one compiled query."""

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
        compiled: CompiledPostgresQuery,
        expectation: LiveQueryExpectation,
        max_rows: int,
        row_consumer: RowConsumer,
        cancellation: PostgresExecutionCancellation | None = None,
    ) -> RemotePostgresExecution:
        """Run only the compiler-produced statement in one read-only transaction.

        ``row_consumer`` serializes and retains rows one at a time.  Returning
        false stops the cursor without retaining the candidate row, which is
        how the service enforces its serialized-byte cap without slicing data.
        """

        if not _is_compiler_minted_postgres_query(compiled) or max_rows < 1:
            raise PostgresExecutionConfigurationError()
        if cancellation is not None and cancellation.cancelled:
            raise PostgresExecutionCancelledError()

        relations = tuple(
            sorted(
                _relation_expectations_for_compiled(compiled, expectation),
                key=lambda relation: (relation.schema_name, relation.table_name),
            )
        )
        if not 1 <= len(relations) <= 2:
            raise PostgresExecutionSchemaChangedError()

        async def body(connection: Any) -> RemotePostgresExecution:
            async def check_unique_key(relation: LiveRelationExpectation, oid: int) -> None:
                if relation.unique_key is not None:
                    live_key = await connection.fetch(
                        _LIVE_UNIQUE_KEY_SQL,
                        oid,
                        relation.unique_key.constraint_name,
                    )
                    _validate_live_unique_key(live_key, relation.unique_key)

            before_oids = await self._verify_and_lock_relations(
                connection, relations, after_columns=check_unique_key
            )

            if expectation.foreign_key is not None:
                lookup_relation = expectation.lookup_relation
                if lookup_relation is None:
                    raise PostgresExecutionSchemaChangedError()
                live_foreign_key = await connection.fetch(
                    _LIVE_FOREIGN_KEY_SQL,
                    before_oids[(expectation.schema_name, expectation.table_name)],
                    before_oids[(lookup_relation.schema_name, lookup_relation.table_name)],
                    expectation.foreign_key.constraint_name,
                )
                _validate_live_foreign_key(live_foreign_key, expectation.foreign_key)

            statement = await _maybe_await(connection.prepare(compiled.sql))
            actual_columns = _prepared_attributes(statement, expectation.expected_output_columns)
            aliases = tuple(column.alias for column in actual_columns)
            cursor = statement.cursor(
                *compiled.parameters,
                prefetch=self._settings.postgres_execution_cursor_prefetch,
            )
            safety_row_truncated = False
            collector_stopped = False
            accepted_rows = 0
            async for record in cursor:
                if cancellation is not None and cancellation.cancelled:
                    raise PostgresExecutionCancelledError()
                # This is bounded lookahead.  The compiler's LIMIT is
                # untouched, so aggregates retain their answer meaning.
                if accepted_rows >= max_rows:
                    safety_row_truncated = True
                    break
                should_continue = await _maybe_await(row_consumer(_record_values(record, aliases)))
                if should_continue is not True:
                    collector_stopped = True
                    break
                accepted_rows += 1
            return RemotePostgresExecution(
                actual_output_columns=actual_columns,
                safety_row_truncated=safety_row_truncated,
                collector_stopped=collector_stopped,
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

    async def execute_sql(
        self,
        *,
        host: str,
        port: int,
        database_name: str,
        username: str,
        password: str,
        custom_ca_pem: str | None,
        sealed: SealedValidatedQuery,
        expectation: LiveSqlExpectation,
        max_rows: int,
        row_consumer: RowConsumer,
        on_columns: Callable[[tuple[ActualPostgresOutputColumn, ...]], None] | None = None,
        max_plan_cost: float | None = None,
        cancellation: PostgresExecutionCancellation | None = None,
    ) -> RemoteSqlExecution:
        """Run one validated, sealed SELECT (SPEC-05 B3).

        Order of checks, all in one read-only transaction: lock every relation (canonical order) ->
        live relation + column checks -> prepare -> output-type gate -> EXPLAIN cost gate -> cursor.
        Nothing reaches the cursor if a gate refuses. ``row_consumer`` receives positional values.
        """

        if not is_sealed(sealed) or max_rows < 1:
            raise PostgresExecutionConfigurationError()
        if not isinstance(expectation, LiveSqlExpectation):
            raise PostgresExecutionConfigurationError()
        if cancellation is not None and cancellation.cancelled:
            raise PostgresExecutionCancelledError()
        ceiling = self._settings.postgres_sql_max_plan_cost if max_plan_cost is None else max_plan_cost

        relations = tuple(
            sorted(expectation.relations, key=lambda relation: (relation.schema_name, relation.table_name))
        )
        identities = {(relation.schema_name, relation.table_name) for relation in relations}
        # The facts must describe exactly the tables the sealed query reads: no more, no fewer.
        # At least one: the validator rejects table-less SQL (``no_table_reference``), so none is a bug.
        if (
            not 1 <= len(relations) <= MAX_SQL_RELATIONS
            or len(identities) != len(relations)
            or identities != {(table.schema, table.name) for table in sealed.tables}
        ):
            raise PostgresExecutionConfigurationError()
        sql = sealed.sql   # I2: the only text sent is the regenerated text.

        async def body(connection: Any) -> RemoteSqlExecution:
            await self._verify_and_lock_relations(connection, relations)
            try:
                return await statements(connection)
            except (PostgresExecutionAdapterError, asyncio.CancelledError):
                raise
            except Exception as exc:
                # A rewritable server refusal becomes a SQLSTATE-only ``query_error`` (SPEC-09 W1.9). The live
                # relation checks above stay untouched, so a real schema change is still ``schema_changed``.
                mapped = _query_error(exc, cancellation)
                if mapped is None:
                    raise
                raise mapped from None

        async def statements(connection: Any) -> RemoteSqlExecution:
            # The size guard wraps the sealed text; the gates below see what will actually run.
            run_sql = _wrap_row_size_guard(sql, self._settings.postgres_sql_max_row_bytes)
            try:
                statement = await _maybe_await(connection.prepare(run_sql))
            except asyncpg.exceptions.UnsupportedClientFeatureError:
                # With text codecs installed asyncpg cannot build a decoder for a composite output
                # (whole-row `SELECT o FROM t o`) while preparing. Same refusal as the type gate, without
                # a column position: asyncpg never got as far as reading the attributes.
                raise PostgresExecutionUnsupportedOutputError(0, "composite") from None
            columns = _sql_output_columns(statement)
            plan_cost = _plan_total_cost(await connection.fetchval(f"EXPLAIN (FORMAT JSON) {run_sql}"))
            if plan_cost > ceiling:
                raise PostgresExecutionCostExceededError()
            if on_columns is not None:
                on_columns(columns)

            cursor = statement.cursor(prefetch=self._settings.postgres_execution_cursor_prefetch)
            safety_row_truncated = False
            collector_stopped = False
            accepted_rows = 0
            async for record in cursor:
                if cancellation is not None and cancellation.cancelled:
                    raise PostgresExecutionCancelledError()
                if accepted_rows >= max_rows:
                    # Only the "no LIMIT / LIMIT above the cap" shape (cap + 1) can overshoot; a model
                    # LIMIT within the cap is the answer's own meaning, not a safety truncation.
                    safety_row_truncated = sealed.applied_limit == sealed.row_cap + 1
                    break
                # By position, never by name: asyncpg returns the last of duplicate names (F4).
                values = tuple(record)
                if len(values) != len(columns):
                    raise PostgresExecutionSchemaChangedError()
                should_continue = await _maybe_await(row_consumer(values))
                if should_continue is not True:
                    collector_stopped = True
                    break
                accepted_rows += 1
            return RemoteSqlExecution(
                actual_output_columns=columns,
                plan_cost=plan_cost,
                safety_row_truncated=safety_row_truncated,
                collector_stopped=collector_stopped,
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

    @staticmethod
    async def _verify_and_lock_relations(
        connection: Any,
        relations: tuple[LiveRelationExpectation, ...],
        after_columns: Callable[[LiveRelationExpectation, int], Awaitable[None]] | None = None,
    ) -> dict[tuple[str, str], int]:
        """Identity-check, lock in canonical order, then re-check each relation and its live columns."""

        before_oids: dict[tuple[str, str], int] = {}
        for relation in relations:
            before = await connection.fetchrow(
                _LIVE_RELATION_SQL,
                relation.schema_name,
                relation.table_name,
            )
            if before is None:
                raise PostgresExecutionSchemaChangedError()
            before_oids[(relation.schema_name, relation.table_name)] = _validate_restricted_relation(
                before, relation
            )

        # Lock in a single canonical order. This prevents two allowed joins requested in
        # opposite directions from creating an application-induced DDL lock cycle.
        for relation in relations:
            lock_sql = (
                "LOCK TABLE "
                f"{_quote_identifier(relation.schema_name)}."
                f"{_quote_identifier(relation.table_name)} IN ACCESS SHARE MODE"
            )
            await connection.execute(lock_sql)

        for relation in relations:
            relation_key = (relation.schema_name, relation.table_name)
            after = await connection.fetchrow(
                _LIVE_RELATION_SQL,
                relation.schema_name,
                relation.table_name,
            )
            if after is None or _validate_restricted_relation(after, relation) != before_oids[relation_key]:
                raise PostgresExecutionSchemaChangedError()
            live_columns = (
                await connection.fetch(
                    _LIVE_COLUMNS_SQL,
                    before_oids[relation_key],
                    [column.name for column in relation.columns],
                )
                if relation.columns
                else ()
            )
            _validate_live_columns(live_columns, relation)
            if after_columns is not None:
                await after_columns(relation, before_oids[relation_key])
        return before_oids

    async def _run_in_session(
        self,
        *,
        host: str,
        port: int,
        database_name: str,
        username: str,
        password: str,
        custom_ca_pem: str | None,
        cancellation: PostgresExecutionCancellation | None,
        body: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Open one constrained connection and run ``body`` in its read-only transaction."""

        connection: Any | None = None
        abort_connection = False
        try:
            async with asyncio.timeout(self._settings.postgres_execution_operation_timeout_seconds):
                normalized_host = await self._policy.validate(host=host, port=port)
                ssl_context = self._policy.build_ssl_context(custom_ca_pem)
                connection = await self._connect(
                    host=normalized_host,
                    port=port,
                    database=database_name,
                    user=username,
                    password=password,
                    ssl=ssl_context,
                    timeout=self._settings.postgres_execution_connect_timeout_seconds,
                    command_timeout=self._settings.postgres_execution_statement_timeout_seconds,
                    server_settings={
                        "application_name": "neuronova-postgres-query",
                        "default_transaction_read_only": "on",
                        "search_path": "pg_catalog",
                        "TimeZone": "UTC",
                        "DateStyle": "ISO, YMD",
                        "statement_timeout": str(
                            int(self._settings.postgres_execution_statement_timeout_seconds * 1000)
                        ),
                        "lock_timeout": str(
                            int(self._settings.postgres_execution_lock_timeout_seconds * 1000)
                        ),
                        "idle_in_transaction_session_timeout": str(
                            int(self._settings.postgres_execution_idle_transaction_timeout_seconds * 1000)
                        ),
                    },
                )
                if cancellation is not None:
                    await cancellation.bind(connection)
                await _install_text_temporal_codecs(connection)
                async with connection.transaction(readonly=True, isolation="read_committed"):
                    # Fixed application-owned statements; no remote metadata is
                    # ever interpolated into these settings.
                    await connection.execute(_SESSION_SEARCH_PATH_SQL)
                    await connection.execute(_SESSION_TIME_ZONE_SQL)
                    await connection.execute(
                        _session_timeout_sql(
                            "statement_timeout",
                            int(self._settings.postgres_execution_statement_timeout_seconds * 1000),
                        )
                    )
                    await connection.execute(
                        _session_timeout_sql(
                            "lock_timeout",
                            int(self._settings.postgres_execution_lock_timeout_seconds * 1000),
                        )
                    )
                    await connection.execute(
                        _session_timeout_sql(
                            "idle_in_transaction_session_timeout",
                            int(
                                self._settings.postgres_execution_idle_transaction_timeout_seconds
                                * 1000
                            ),
                        )
                    )
                    _validate_session_assumptions(await connection.fetchrow(_SESSION_ASSERTIONS_SQL))
                    # Terminating our socket does not stop a running query by itself. With this
                    # (PostgreSQL 14+) the backend notices the dead client and aborts instead of running
                    # to the statement timeout. Applies to both the compiled and the SQL path. On older
                    # servers the setting does not exist and the statement is a no-op SELECT.
                    await connection.execute(_SESSION_CLIENT_CHECK_SQL)
                    return await body(connection)
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
            if _is_row_too_large(exc):
                raise PostgresExecutionRowTooLargeError() from None
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
            # Raw driver exceptions can include host, role, SQL, or values.
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
    "ActualPostgresOutputColumn",
    "LiveColumnExpectation",
    "LiveForeignKeyExpectation",
    "LiveRelationExpectation",
    "LiveQueryExpectation",
    "LiveUniqueKeyExpectation",
    "PostgresExecutionAccessChangedError",
    "PostgresExecutionAdapterError",
    "PostgresExecutionCancellation",
    "PostgresExecutionCancelledError",
    "PostgresExecutionConfigurationError",
    "PostgresExecutionCostExceededError",
    "PostgresExecutionCredentialError",
    "PostgresExecutionRestrictedRoleError",
    "PostgresExecutionQueryError",
    "PostgresExecutionRowTooLargeError",
    "PostgresExecutionSchemaChangedError",
    "PostgresExecutionTLSFailureError",
    "PostgresExecutionTimeoutError",
    "PostgresExecutionUnsupportedOutputError",
    "PostgresQueryExecutionAdapter",
    "LiveSqlExpectation",
    "RemotePostgresExecution",
    "RemoteSqlExecution",
    "REPAIRABLE_SQLSTATES",
    "SUPPORTED_OUTPUT_TYPES",
    "dedupe_output_labels",
]
