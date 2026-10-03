"""Restricted, internal PostgreSQL execution transport for Stage 4B.

This module owns only a short-lived *client database* connection.  It knows
nothing about HTTP, LLMs, application-database sessions, or source
credentials at rest.  Callers supply a compiler-produced statement and facts
resolved from the current authorized catalog; they never supply SQL text.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.connectors.connection_policy import ConnectionPolicyError, PostgresConnectionPolicy
from app.core.config import Settings
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
class LiveQueryExpectation:
    """Trusted structural facts to recheck in the remote transaction."""

    schema_name: str
    table_name: str
    columns: tuple[LiveColumnExpectation, ...]
    expected_output_columns: tuple[ExpectedPostgresOutputColumn, ...]


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

_SESSION_SEARCH_PATH_SQL = "SET LOCAL search_path TO pg_catalog"
_SESSION_TIME_ZONE_SQL = "SET LOCAL TimeZone TO 'UTC'"
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


def _validate_restricted_relation(row: Any, expectation: LiveQueryExpectation) -> int:
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
        _row_value(row, "relation_kind") != "r"
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


def _validate_live_columns(rows: Any, expectation: LiveQueryExpectation) -> None:
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
                    _validate_session_assumptions(
                        await connection.fetchrow(_SESSION_ASSERTIONS_SQL)
                    )
                    before = await connection.fetchrow(
                        _LIVE_RELATION_SQL, expectation.schema_name, expectation.table_name
                    )
                    if before is None:
                        raise PostgresExecutionSchemaChangedError()
                    before_oid = _validate_restricted_relation(before, expectation)

                    lock_sql = (
                        "LOCK TABLE "
                        f"{_quote_identifier(expectation.schema_name)}."
                        f"{_quote_identifier(expectation.table_name)} IN ACCESS SHARE MODE"
                    )
                    await connection.execute(lock_sql)
                    after = await connection.fetchrow(
                        _LIVE_RELATION_SQL, expectation.schema_name, expectation.table_name
                    )
                    if after is None or _validate_restricted_relation(after, expectation) != before_oid:
                        raise PostgresExecutionSchemaChangedError()
                    live_columns = (
                        await connection.fetch(
                            _LIVE_COLUMNS_SQL,
                            before_oid,
                            [column.name for column in expectation.columns],
                        )
                        if expectation.columns
                        else ()
                    )
                    _validate_live_columns(live_columns, expectation)

                    statement = await _maybe_await(connection.prepare(compiled.sql))
                    actual_columns = _prepared_attributes(
                        statement, expectation.expected_output_columns
                    )
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
                            abort_connection = True
                            raise PostgresExecutionCancelledError()
                        # This is bounded lookahead.  The compiler's LIMIT is
                        # untouched, so aggregates retain their answer meaning.
                        if accepted_rows >= max_rows:
                            safety_row_truncated = True
                            break
                        should_continue = await _maybe_await(
                            row_consumer(_record_values(record, aliases))
                        )
                        if should_continue is not True:
                            collector_stopped = True
                            break
                        accepted_rows += 1
                    return RemotePostgresExecution(
                        actual_output_columns=actual_columns,
                        safety_row_truncated=safety_row_truncated,
                        collector_stopped=collector_stopped,
                    )
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
    "LiveQueryExpectation",
    "PostgresExecutionAccessChangedError",
    "PostgresExecutionAdapterError",
    "PostgresExecutionCancellation",
    "PostgresExecutionCancelledError",
    "PostgresExecutionConfigurationError",
    "PostgresExecutionCredentialError",
    "PostgresExecutionRestrictedRoleError",
    "PostgresExecutionSchemaChangedError",
    "PostgresExecutionTLSFailureError",
    "PostgresExecutionTimeoutError",
    "PostgresQueryExecutionAdapter",
    "RemotePostgresExecution",
]
