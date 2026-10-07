"""Short-lived, read-only PostgreSQL onboarding adapter.

No application-database URL or SQLAlchemy engine is accepted here. The only
operation is a TLS connection test plus bounded names-only inventory.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Sequence

import asyncpg

from app.connectors.connection_policy import ConnectionPolicyError, PostgresConnectionPolicy
from app.core.config import Settings


class PostgresAdapterError(Exception):
    code = "connection_failed"


class PostgresTimeoutError(PostgresAdapterError):
    code = "connection_timeout"


class PostgresTLSFailure(PostgresAdapterError):
    code = "tls_verification_failed"


class InventoryOverflowError(PostgresAdapterError):
    code = "inventory_limit_exceeded"


class InaccessibleInventoryError(PostgresAdapterError):
    code = "no_eligible_tables"


class PostgresCredentialFailure(PostgresAdapterError):
    code = "invalid_credentials"


class MetadataDiscoveryTimeoutError(PostgresAdapterError):
    code = "metadata_timeout"


class SelectedTableUnavailableError(PostgresAdapterError):
    code = "selected_table_unavailable"


class MetadataAccessError(PostgresAdapterError):
    code = "metadata_access_denied"


class MetadataLimitError(PostgresAdapterError):
    """A complete snapshot cannot be built within a configured safe limit."""

    def __init__(self, code: str):
        self.code = code
        super().__init__()


@dataclass(frozen=True, order=True)
class InventoryTable:
    schema_name: str
    table_name: str


@dataclass(frozen=True)
class DiscoveredColumn:
    column_name: str
    ordinal_position: int
    is_nullable: bool
    formatted_native_type: str
    native_type_schema: str | None
    native_type_name: str | None
    native_type_oid: int | None
    native_type_modifier: int | None
    column_comment: str | None


@dataclass(frozen=True)
class DiscoveredKey:
    constraint_name: str
    key_kind: str
    column_names: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveredForeignKeyMapping:
    ordinal_position: int
    referencing_column_name: str
    referenced_column_name: str


@dataclass(frozen=True)
class DiscoveredForeignKey:
    constraint_name: str
    target_in_scope: bool
    referenced_schema_name: str | None
    referenced_table_name: str | None
    column_mappings: tuple[DiscoveredForeignKeyMapping, ...]


@dataclass(frozen=True)
class DiscoveredTable:
    schema_name: str
    table_name: str
    table_comment: str | None
    columns: tuple[DiscoveredColumn, ...]
    declared_keys: tuple[DiscoveredKey, ...]
    foreign_keys: tuple[DiscoveredForeignKey, ...]


# ``has_table_privilege`` requires table-level SELECT and the second predicate
# checks every current column. We intentionally exclude column-only grants:
# later source queries cannot safely assume an arbitrary column is readable.
_NAMES_ONLY_INVENTORY_SQL = """
SELECT table_schema, table_name
FROM information_schema.tables AS tables
WHERE tables.table_type = 'BASE TABLE'
  AND tables.table_schema NOT IN ('pg_catalog', 'information_schema')
  AND tables.table_schema NOT LIKE 'pg\\_%' ESCAPE '\\'
  AND has_schema_privilege(tables.table_schema, 'USAGE')
  AND has_table_privilege(
      format('%I.%I', tables.table_schema, tables.table_name), 'SELECT'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM information_schema.columns AS columns
      WHERE columns.table_schema = tables.table_schema
        AND columns.table_name = tables.table_name
        AND NOT has_column_privilege(
            format('%I.%I', columns.table_schema, columns.table_name),
            columns.column_name,
            'SELECT'
        )
  )
ORDER BY tables.table_schema, tables.table_name
LIMIT $1
"""


# Every discovery query starts with this bounded scope CTE. PostgreSQL catalog
# names are compared as values, never interpolated into an identifier or SQL
# fragment. These queries inspect only metadata catalogs; no client table rows
# are selected.
_SELECTED_SCOPE_CTE = """
WITH selected(schema_name, table_name) AS (
    SELECT * FROM unnest($1::text[], $2::text[])
)
"""

_SELECTED_ELIGIBILITY_SQL = _SELECTED_SCOPE_CTE + """
SELECT selected.schema_name, selected.table_name
FROM selected
JOIN information_schema.tables AS tables
  ON tables.table_schema = selected.schema_name
 AND tables.table_name = selected.table_name
WHERE tables.table_type = 'BASE TABLE'
  AND tables.table_schema NOT IN ('pg_catalog', 'information_schema')
  AND tables.table_schema NOT LIKE 'pg\\_%' ESCAPE '\\'
  AND has_schema_privilege(tables.table_schema, 'USAGE')
  AND has_table_privilege(
      format('%I.%I', tables.table_schema, tables.table_name), 'SELECT'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM information_schema.columns AS columns
      WHERE columns.table_schema = tables.table_schema
        AND columns.table_name = tables.table_name
        AND NOT has_column_privilege(
            format('%I.%I', columns.table_schema, columns.table_name),
            columns.column_name,
            'SELECT'
        )
  )
ORDER BY selected.schema_name, selected.table_name
"""

_SELECTED_TABLES_SQL = _SELECTED_SCOPE_CTE + """
, assets AS (
    SELECT selected.schema_name, selected.table_name,
           obj_description(classes.oid, 'pg_class') AS table_comment
    FROM selected
    JOIN pg_catalog.pg_namespace AS namespaces
      ON namespaces.nspname = selected.schema_name
    JOIN pg_catalog.pg_class AS classes
      ON classes.relnamespace = namespaces.oid
     AND classes.relname = selected.table_name
     AND classes.relkind IN ('r', 'p')
)
SELECT schema_name, table_name,
       CASE WHEN table_comment IS NULL OR octet_length(table_comment) <= $3
            THEN table_comment ELSE NULL END AS table_comment,
       COALESCE(octet_length(table_comment), 0) > $3 AS comment_too_large
FROM assets
ORDER BY schema_name, table_name
"""

_SELECTED_COLUMNS_SQL = _SELECTED_SCOPE_CTE + """
, columns AS (
    SELECT selected.schema_name, selected.table_name, attributes.attname AS column_name,
           attributes.attnum AS ordinal_position, NOT attributes.attnotnull AS is_nullable,
           pg_catalog.format_type(attributes.atttypid, attributes.atttypmod) AS formatted_native_type,
           type_namespaces.nspname AS native_type_schema, types.typname AS native_type_name,
           attributes.atttypid::integer AS native_type_oid,
           attributes.atttypmod AS native_type_modifier,
           col_description(classes.oid, attributes.attnum) AS column_comment
    FROM selected
    JOIN pg_catalog.pg_namespace AS namespaces
      ON namespaces.nspname = selected.schema_name
    JOIN pg_catalog.pg_class AS classes
      ON classes.relnamespace = namespaces.oid
     AND classes.relname = selected.table_name
     AND classes.relkind IN ('r', 'p')
    JOIN pg_catalog.pg_attribute AS attributes
      ON attributes.attrelid = classes.oid
     AND attributes.attnum > 0
     AND NOT attributes.attisdropped
    JOIN pg_catalog.pg_type AS types ON types.oid = attributes.atttypid
    LEFT JOIN pg_catalog.pg_namespace AS type_namespaces ON type_namespaces.oid = types.typnamespace
)
SELECT schema_name, table_name, column_name, ordinal_position, is_nullable,
       formatted_native_type, native_type_schema, native_type_name,
       native_type_oid, native_type_modifier,
       CASE WHEN column_comment IS NULL OR octet_length(column_comment) <= $3
            THEN column_comment ELSE NULL END AS column_comment,
       COALESCE(octet_length(column_comment), 0) > $3 AS comment_too_large
FROM columns
ORDER BY schema_name, table_name, ordinal_position
LIMIT $4
"""

_SELECTED_KEYS_SQL = _SELECTED_SCOPE_CTE + """
SELECT selected.schema_name, selected.table_name, constraints.conname AS constraint_name,
       constraints.contype AS key_kind, key_columns.ordinal_position,
       attributes.attname AS column_name
FROM selected
JOIN pg_catalog.pg_namespace AS namespaces
  ON namespaces.nspname = selected.schema_name
JOIN pg_catalog.pg_class AS classes
  ON classes.relnamespace = namespaces.oid
 AND classes.relname = selected.table_name
 AND classes.relkind IN ('r', 'p')
JOIN pg_catalog.pg_constraint AS constraints
  ON constraints.conrelid = classes.oid
 AND constraints.contype IN ('p', 'u')
JOIN LATERAL unnest(constraints.conkey) WITH ORDINALITY
  AS key_columns(attnum, ordinal_position) ON TRUE
JOIN pg_catalog.pg_attribute AS attributes
  ON attributes.attrelid = classes.oid
 AND attributes.attnum = key_columns.attnum
ORDER BY selected.schema_name, selected.table_name, constraints.conname, key_columns.ordinal_position
LIMIT $3
"""

_SELECTED_FOREIGN_KEYS_SQL = _SELECTED_SCOPE_CTE + """
SELECT selected.schema_name, selected.table_name, constraints.conname AS constraint_name,
       target_selected.schema_name AS referenced_schema_name,
       target_selected.table_name AS referenced_table_name,
       key_columns.ordinal_position,
       local_attributes.attname AS referencing_column_name,
       CASE WHEN target_selected.schema_name IS NOT NULL THEN target_attributes.attname ELSE NULL END
         AS referenced_column_name
FROM selected
JOIN pg_catalog.pg_namespace AS namespaces
  ON namespaces.nspname = selected.schema_name
JOIN pg_catalog.pg_class AS classes
  ON classes.relnamespace = namespaces.oid
 AND classes.relname = selected.table_name
 AND classes.relkind IN ('r', 'p')
JOIN pg_catalog.pg_constraint AS constraints
  ON constraints.conrelid = classes.oid
 AND constraints.contype = 'f'
JOIN LATERAL unnest(constraints.conkey, constraints.confkey) WITH ORDINALITY
  AS key_columns(local_attnum, target_attnum, ordinal_position) ON TRUE
JOIN pg_catalog.pg_attribute AS local_attributes
  ON local_attributes.attrelid = classes.oid
 AND local_attributes.attnum = key_columns.local_attnum
LEFT JOIN pg_catalog.pg_class AS target_classes ON target_classes.oid = constraints.confrelid
LEFT JOIN pg_catalog.pg_namespace AS target_namespaces ON target_namespaces.oid = target_classes.relnamespace
LEFT JOIN selected AS target_selected
  ON target_selected.schema_name = target_namespaces.nspname
 AND target_selected.table_name = target_classes.relname
LEFT JOIN pg_catalog.pg_attribute AS target_attributes
  ON target_selected.schema_name IS NOT NULL
 AND target_attributes.attrelid = constraints.confrelid
 AND target_attributes.attnum = key_columns.target_attnum
ORDER BY selected.schema_name, selected.table_name, constraints.conname, key_columns.ordinal_position
LIMIT $3
"""


ConnectFn = Callable[..., Awaitable[Any]]


async def _close_quietly(connection: Any) -> None:
    try:
        result = connection.close()
        if inspect.isawaitable(result):
            await asyncio.shield(result)
    except Exception:
        # Cleanup must not replace a safe primary failure. Do not log a driver
        # exception string here because it can include connection context.
        pass


class PostgresOnboardingAdapter:
    def __init__(
        self,
        *,
        settings: Settings,
        policy: PostgresConnectionPolicy | None = None,
        connect: ConnectFn = asyncpg.connect,
    ):
        self._settings = settings
        self._policy = policy or PostgresConnectionPolicy()
        self._connect = connect

    async def test_and_list_tables(
        self,
        *,
        host: str,
        port: int,
        database_name: str,
        username: str,
        password: str,
        custom_ca_pem: str | None,
    ) -> list[InventoryTable]:
        connection: Any | None = None
        try:
            # Destination validation can perform DNS resolution. It belongs in
            # the same bounded operation as the handshake and inventory so a
            # stalled resolver cannot occupy an outbound slot indefinitely.
            async with asyncio.timeout(self._settings.connector_operation_timeout_seconds):
                normalized_host = await self._policy.validate(host=host, port=port)
                ssl_context = self._policy.build_ssl_context(custom_ca_pem)
                connection = await self._connect(
                    host=normalized_host,
                    port=port,
                    database=database_name,
                    user=username,
                    password=password,
                    ssl=ssl_context,
                    timeout=self._settings.connector_connect_timeout_seconds,
                    command_timeout=self._settings.connector_statement_timeout_seconds,
                    server_settings={
                        "application_name": "neuronova-source-onboarding",
                        "default_transaction_read_only": "on",
                        "statement_timeout": str(
                            int(self._settings.connector_statement_timeout_seconds * 1000)
                        ),
                    },
                )
                async with connection.transaction(readonly=True):
                    rows = await connection.fetch(
                        _NAMES_ONLY_INVENTORY_SQL,
                        self._settings.connector_max_inventory_tables + 1,
                    )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise PostgresTimeoutError() from None
        except ConnectionPolicyError:
            raise
        except Exception as exc:
            # Never retain/return raw asyncpg exceptions: they can contain
            # hostname, role, or backend details. TLS failures have a useful,
            # safe category; every other driver failure stays generic.
            if exc.__class__.__module__.startswith("ssl"):
                raise PostgresTLSFailure() from None
            raise PostgresAdapterError() from None
        finally:
            if connection is not None:
                await _close_quietly(connection)

        if len(rows) > self._settings.connector_max_inventory_tables:
            raise InventoryOverflowError()
        tables = [
            InventoryTable(schema_name=str(row["table_schema"]), table_name=str(row["table_name"]))
            for row in rows
        ]
        if not tables:
            raise InaccessibleInventoryError()
        return tables

    async def discover_selected_tables(
        self,
        *,
        host: str,
        port: int,
        database_name: str,
        username: str,
        password: str,
        custom_ca_pem: str | None,
        selected_tables: Sequence[InventoryTable],
    ) -> tuple[DiscoveredTable, ...]:
        """Read a bounded, consistent structural snapshot of the saved scope.

        ``selected_tables`` came from persisted source scope, not an HTTP body.
        Each query is restricted to that scope through bound array parameters.
        This intentionally never selects a row from a client relation.
        """

        scope = _normalize_scope(selected_tables)
        if len(scope) > self._settings.catalog_discovery_max_tables:
            raise MetadataLimitError("metadata_table_limit_exceeded")
        schema_names = [item.schema_name for item in scope]
        table_names = [item.table_name for item in scope]
        connection: Any | None = None
        try:
            async with asyncio.timeout(self._settings.catalog_discovery_timeout_seconds):
                normalized_host = await self._policy.validate(host=host, port=port)
                ssl_context = self._policy.build_ssl_context(custom_ca_pem)
                connection = await self._connect(
                    host=normalized_host,
                    port=port,
                    database=database_name,
                    user=username,
                    password=password,
                    ssl=ssl_context,
                    timeout=self._settings.connector_connect_timeout_seconds,
                    command_timeout=self._settings.connector_statement_timeout_seconds,
                    server_settings={
                        "application_name": "neuronova-catalog-discovery",
                        "default_transaction_read_only": "on",
                        "statement_timeout": str(
                            int(self._settings.connector_statement_timeout_seconds * 1000)
                        ),
                    },
                )
                async with connection.transaction(readonly=True, isolation="repeatable_read"):
                    eligible_rows = await connection.fetch(
                        _SELECTED_ELIGIBILITY_SQL, schema_names, table_names
                    )
                    eligible = {
                        (str(row["schema_name"]), str(row["table_name"]))
                        for row in eligible_rows
                    }
                    expected = {(item.schema_name, item.table_name) for item in scope}
                    if eligible != expected:
                        raise SelectedTableUnavailableError()

                    table_rows = await connection.fetch(
                        _SELECTED_TABLES_SQL,
                        schema_names,
                        table_names,
                        self._settings.catalog_discovery_max_metadata_bytes,
                    )
                    column_rows = await connection.fetch(
                        _SELECTED_COLUMNS_SQL,
                        schema_names,
                        table_names,
                        self._settings.catalog_discovery_max_metadata_bytes,
                        self._settings.catalog_discovery_max_columns + 1,
                    )
                    key_rows = await connection.fetch(
                        _SELECTED_KEYS_SQL,
                        schema_names,
                        table_names,
                        self._settings.catalog_discovery_max_columns + 1,
                    )
                    foreign_key_rows = await connection.fetch(
                        _SELECTED_FOREIGN_KEYS_SQL,
                        schema_names,
                        table_names,
                        self._settings.catalog_discovery_max_relationships + 1,
                    )
        except asyncio.CancelledError:
            raise
        except MetadataLimitError:
            raise
        except SelectedTableUnavailableError:
            raise
        except TimeoutError:
            raise MetadataDiscoveryTimeoutError() from None
        except ConnectionPolicyError:
            raise
        except Exception as exc:
            if isinstance(
                exc,
                (
                    asyncpg.InvalidPasswordError,
                    asyncpg.InvalidAuthorizationSpecificationError,
                ),
            ):
                raise PostgresCredentialFailure() from None
            if isinstance(exc, asyncpg.InsufficientPrivilegeError):
                raise MetadataAccessError() from None
            if exc.__class__.__module__.startswith("ssl"):
                raise PostgresTLSFailure() from None
            raise PostgresAdapterError() from None
        finally:
            if connection is not None:
                await _close_quietly(connection)

        return _normalize_discovery(
            scope=scope,
            table_rows=table_rows,
            column_rows=column_rows,
            key_rows=key_rows,
            foreign_key_rows=foreign_key_rows,
            max_columns=self._settings.catalog_discovery_max_columns,
            max_relationships=self._settings.catalog_discovery_max_relationships,
            max_metadata_bytes=self._settings.catalog_discovery_max_metadata_bytes,
        )


def _normalize_scope(selected_tables: Sequence[InventoryTable]) -> tuple[InventoryTable, ...]:
    scope = tuple(sorted(selected_tables))
    if not scope or len({(item.schema_name, item.table_name) for item in scope}) != len(scope):
        raise SelectedTableUnavailableError()
    return scope


def _normalize_discovery(
    *,
    scope: tuple[InventoryTable, ...],
    table_rows: Sequence[Any],
    column_rows: Sequence[Any],
    key_rows: Sequence[Any],
    foreign_key_rows: Sequence[Any],
    max_columns: int,
    max_relationships: int,
    max_metadata_bytes: int,
) -> tuple[DiscoveredTable, ...]:
    """Map catalog rows into a complete structural representation.

    All result-shape checks fail closed. Returning a shortened structure would
    incorrectly claim a complete immutable catalog snapshot.
    """

    expected = {(item.schema_name, item.table_name) for item in scope}
    if len(table_rows) != len(scope):
        raise SelectedTableUnavailableError()
    if len(column_rows) > max_columns or len(key_rows) > max_columns:
        raise MetadataLimitError("metadata_column_limit_exceeded")
    if len(foreign_key_rows) > max_relationships:
        raise MetadataLimitError("metadata_relationship_limit_exceeded")

    metadata_bytes = 0

    def text(value: Any) -> str | None:
        nonlocal metadata_bytes
        if value is None:
            return None
        result = str(value)
        metadata_bytes += len(result.encode("utf-8"))
        if metadata_bytes > max_metadata_bytes:
            raise MetadataLimitError("metadata_bytes_limit_exceeded")
        return result

    tables: dict[tuple[str, str], dict[str, Any]] = {}
    for row in table_rows:
        if bool(row["comment_too_large"]):
            raise MetadataLimitError("metadata_bytes_limit_exceeded")
        schema_name = text(row["schema_name"])
        table_name = text(row["table_name"])
        if schema_name is None or table_name is None:
            raise SelectedTableUnavailableError()
        identity = (schema_name, table_name)
        if identity not in expected or identity in tables:
            raise SelectedTableUnavailableError()
        tables[identity] = {
            "comment": text(row["table_comment"]),
            "columns": [],
            "keys": {},
            "foreign_keys": {},
        }
    if set(tables) != expected:
        raise SelectedTableUnavailableError()

    for row in column_rows:
        if bool(row["comment_too_large"]):
            raise MetadataLimitError("metadata_bytes_limit_exceeded")
        identity = (str(row["schema_name"]), str(row["table_name"]))
        table = tables.get(identity)
        if table is None:
            raise SelectedTableUnavailableError()
        column_name = text(row["column_name"])
        formatted_native_type = text(row["formatted_native_type"])
        if column_name is None or formatted_native_type is None:
            raise SelectedTableUnavailableError()
        table["columns"].append(
            DiscoveredColumn(
                column_name=column_name,
                ordinal_position=int(row["ordinal_position"]),
                is_nullable=bool(row["is_nullable"]),
                formatted_native_type=formatted_native_type,
                native_type_schema=text(row["native_type_schema"]),
                native_type_name=text(row["native_type_name"]),
                native_type_oid=(int(row["native_type_oid"]) if row["native_type_oid"] is not None else None),
                native_type_modifier=(
                    int(row["native_type_modifier"])
                    if row["native_type_modifier"] is not None
                    else None
                ),
                column_comment=text(row["column_comment"]),
            )
        )

    for identity, table in tables.items():
        columns = sorted(table["columns"], key=lambda value: value.ordinal_position)
        if not columns or [value.ordinal_position for value in columns] != list(range(1, len(columns) + 1)):
            raise SelectedTableUnavailableError()
        if len({value.column_name for value in columns}) != len(columns):
            raise SelectedTableUnavailableError()
        table["columns"] = columns

    for row in key_rows:
        identity = (str(row["schema_name"]), str(row["table_name"]))
        table = tables.get(identity)
        if table is None:
            raise SelectedTableUnavailableError()
        constraint_name = text(row["constraint_name"])
        column_name = text(row["column_name"])
        raw_kind = row["key_kind"]
        key_kind = raw_kind.decode("ascii") if isinstance(raw_kind, bytes) else str(raw_kind)
        if constraint_name is None or column_name is None or key_kind not in {"p", "u"}:
            raise SelectedTableUnavailableError()
        key = table["keys"].setdefault(
            constraint_name,
            {"kind": key_kind, "columns": []},
        )
        if key["kind"] != key_kind:
            raise SelectedTableUnavailableError()
        key["columns"].append((int(row["ordinal_position"]), column_name))

    for row in foreign_key_rows:
        identity = (str(row["schema_name"]), str(row["table_name"]))
        table = tables.get(identity)
        if table is None:
            raise SelectedTableUnavailableError()
        constraint_name = text(row["constraint_name"])
        referencing_column_name = text(row["referencing_column_name"])
        target_schema = row["referenced_schema_name"]
        target_table = row["referenced_table_name"]
        target_in_scope = target_schema is not None and target_table is not None
        if constraint_name is None or referencing_column_name is None:
            raise SelectedTableUnavailableError()
        foreign_key = table["foreign_keys"].setdefault(
            constraint_name,
            {
                "target_in_scope": target_in_scope,
                "target_schema": None,
                "target_table": None,
                "mappings": [],
            },
        )
        if foreign_key["target_in_scope"] != target_in_scope:
            raise SelectedTableUnavailableError()
        if target_in_scope:
            target_schema_text = text(target_schema)
            target_table_text = text(target_table)
            target_column_name = text(row["referenced_column_name"])
            if (
                target_schema_text is None
                or target_table_text is None
                or target_column_name is None
                or (target_schema_text, target_table_text) not in expected
            ):
                raise SelectedTableUnavailableError()
            if foreign_key["target_schema"] is None:
                foreign_key["target_schema"] = target_schema_text
                foreign_key["target_table"] = target_table_text
            if (
                foreign_key["target_schema"] != target_schema_text
                or foreign_key["target_table"] != target_table_text
            ):
                raise SelectedTableUnavailableError()
            foreign_key["mappings"].append(
                (int(row["ordinal_position"]), referencing_column_name, target_column_name)
            )
        elif row["referenced_column_name"] is not None:
            raise SelectedTableUnavailableError()

    assets: list[DiscoveredTable] = []
    for identity in sorted(tables):
        table = tables[identity]
        column_names = {column.column_name for column in table["columns"]}
        keys: list[DiscoveredKey] = []
        for constraint_name, key in sorted(table["keys"].items()):
            ordered = sorted(key["columns"])
            if (
                not ordered
                or [value[0] for value in ordered] != list(range(1, len(ordered) + 1))
                or len({value[1] for value in ordered}) != len(ordered)
                or not {value[1] for value in ordered}.issubset(column_names)
            ):
                raise SelectedTableUnavailableError()
            keys.append(
                DiscoveredKey(
                    constraint_name=constraint_name,
                    key_kind=key["kind"],
                    column_names=tuple(value[1] for value in ordered),
                )
            )
        foreign_keys: list[DiscoveredForeignKey] = []
        for constraint_name, foreign_key in sorted(table["foreign_keys"].items()):
            if not foreign_key["target_in_scope"]:
                foreign_keys.append(
                    DiscoveredForeignKey(
                        constraint_name=constraint_name,
                        target_in_scope=False,
                        referenced_schema_name=None,
                        referenced_table_name=None,
                        column_mappings=(),
                    )
                )
                continue
            mappings = sorted(foreign_key["mappings"])
            if (
                not mappings
                or [value[0] for value in mappings] != list(range(1, len(mappings) + 1))
                or len({value[1] for value in mappings}) != len(mappings)
            ):
                raise SelectedTableUnavailableError()
            target_columns = {
                column.column_name
                for column in tables[(foreign_key["target_schema"], foreign_key["target_table"])]["columns"]
            }
            if (
                not {value[1] for value in mappings}.issubset(column_names)
                or not {value[2] for value in mappings}.issubset(target_columns)
            ):
                raise SelectedTableUnavailableError()
            foreign_keys.append(
                DiscoveredForeignKey(
                    constraint_name=constraint_name,
                    target_in_scope=True,
                    referenced_schema_name=foreign_key["target_schema"],
                    referenced_table_name=foreign_key["target_table"],
                    column_mappings=tuple(
                        DiscoveredForeignKeyMapping(
                            ordinal_position=ordinal_position,
                            referencing_column_name=referencing_column_name,
                            referenced_column_name=referenced_column_name,
                        )
                        for ordinal_position, referencing_column_name, referenced_column_name in mappings
                    ),
                )
            )
        assets.append(
            DiscoveredTable(
                schema_name=identity[0],
                table_name=identity[1],
                table_comment=table["comment"],
                columns=tuple(table["columns"]),
                declared_keys=tuple(keys),
                foreign_keys=tuple(foreign_keys),
            )
        )
    return tuple(assets)
