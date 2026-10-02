"""Short-lived, read-only PostgreSQL onboarding adapter.

No application-database URL or SQLAlchemy engine is accepted here. The only
operation is a TLS connection test plus bounded names-only inventory.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True, order=True)
class InventoryTable:
    schema_name: str
    table_name: str


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
