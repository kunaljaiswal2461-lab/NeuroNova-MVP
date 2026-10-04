from __future__ import annotations

import asyncio

import pytest

from app.connectors.connection_policy import PostgresConnectionPolicy
from app.connectors.postgres_adapter import (
    InventoryTable,
    MetadataDiscoveryTimeoutError,
    MetadataLimitError,
    PostgresOnboardingAdapter,
    SelectedTableUnavailableError,
)
from app.core.config import Settings


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "api_key": "catalog-discovery-adapter-test-key",
        "database_url": "sqlite+aiosqlite://",
        "catalog_discovery_max_columns": 10,
        "catalog_discovery_max_relationships": 10,
        "catalog_discovery_max_metadata_bytes": 4096,
    }
    values.update(overrides)
    return Settings(**values)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, *, eligible=None, columns=None):
        self.closed = False
        self.calls: list[tuple[str, tuple]] = []
        self.eligible = eligible if eligible is not None else [
            {"schema_name": "sales", "table_name": "customers"},
            {"schema_name": "sales", "table_name": "orders"},
        ]
        self.columns = columns if columns is not None else [
            {
                "schema_name": "sales",
                "table_name": "customers",
                "column_name": "tenant_id",
                "ordinal_position": 1,
                "is_nullable": False,
                "formatted_native_type": "integer",
                "native_type_schema": "pg_catalog",
                "native_type_name": "int4",
                "native_type_oid": 23,
                "native_type_modifier": -1,
                "column_comment": None,
                "comment_too_large": False,
            },
            {
                "schema_name": "sales",
                "table_name": "customers",
                "column_name": "customer_id",
                "ordinal_position": 2,
                "is_nullable": False,
                "formatted_native_type": "integer",
                "native_type_schema": "pg_catalog",
                "native_type_name": "int4",
                "native_type_oid": 23,
                "native_type_modifier": -1,
                "column_comment": "Customer identifier",
                "comment_too_large": False,
            },
            {
                "schema_name": "sales",
                "table_name": "orders",
                "column_name": "order_id",
                "ordinal_position": 1,
                "is_nullable": False,
                "formatted_native_type": "integer",
                "native_type_schema": "pg_catalog",
                "native_type_name": "int4",
                "native_type_oid": 23,
                "native_type_modifier": -1,
                "column_comment": None,
                "comment_too_large": False,
            },
            {
                "schema_name": "sales",
                "table_name": "orders",
                "column_name": "tenant_id",
                "ordinal_position": 2,
                "is_nullable": False,
                "formatted_native_type": "integer",
                "native_type_schema": "pg_catalog",
                "native_type_name": "int4",
                "native_type_oid": 23,
                "native_type_modifier": -1,
                "column_comment": None,
                "comment_too_large": False,
            },
            {
                "schema_name": "sales",
                "table_name": "orders",
                "column_name": "customer_id",
                "ordinal_position": 3,
                "is_nullable": False,
                "formatted_native_type": "integer",
                "native_type_schema": "pg_catalog",
                "native_type_name": "int4",
                "native_type_oid": 23,
                "native_type_modifier": -1,
                "column_comment": None,
                "comment_too_large": False,
            },
        ]

    def transaction(self, *, readonly, isolation):
        assert readonly is True
        assert isolation == "repeatable_read"
        return _Transaction()

    async def fetch(self, sql, *parameters):
        self.calls.append((sql, parameters))
        if "has_table_privilege" in sql:
            return self.eligible
        if "AS table_comment" in sql:
            return [
                {
                    "schema_name": "sales",
                    "table_name": "customers",
                    "table_comment": "Customer table",
                    "comment_too_large": False,
                },
                {
                    "schema_name": "sales",
                    "table_name": "orders",
                    "table_comment": "Order table",
                    "comment_too_large": False,
                },
            ]
        if "AS column_comment" in sql:
            return self.columns
        if "constraints.contype IN ('p', 'u')" in sql:
            return [
                {
                    "schema_name": "sales",
                    "table_name": "customers",
                    "constraint_name": "customers_pkey",
                    "key_kind": "p",
                    "ordinal_position": 1,
                    "column_name": "tenant_id",
                },
                {
                    "schema_name": "sales",
                    "table_name": "customers",
                    "constraint_name": "customers_pkey",
                    "key_kind": "p",
                    "ordinal_position": 2,
                    "column_name": "customer_id",
                },
            ]
        if "constraints.contype = 'f'" in sql:
            return [
                {
                    "schema_name": "sales",
                    "table_name": "orders",
                    "constraint_name": "orders_customer_fkey",
                    "referenced_schema_name": "sales",
                    "referenced_table_name": "customers",
                    "ordinal_position": 1,
                    "referencing_column_name": "tenant_id",
                    "referenced_column_name": "tenant_id",
                },
                {
                    "schema_name": "sales",
                    "table_name": "orders",
                    "constraint_name": "orders_customer_fkey",
                    "referenced_schema_name": "sales",
                    "referenced_table_name": "customers",
                    "ordinal_position": 2,
                    "referencing_column_name": "customer_id",
                    "referenced_column_name": "customer_id",
                },
                {
                    "schema_name": "sales",
                    "table_name": "orders",
                    "constraint_name": "orders_excluded_target_fkey",
                    "referenced_schema_name": None,
                    "referenced_table_name": None,
                    "ordinal_position": 1,
                    "referencing_column_name": "tenant_id",
                    "referenced_column_name": None,
                },
            ]
        raise AssertionError("unexpected metadata query")

    async def close(self):
        self.closed = True


async def _resolver(host: str, port: int) -> list[str]:
    assert host == "client-postgres.stage3b.test"
    assert port == 5432
    return ["127.0.0.1"]


async def test_discovery_uses_bound_scope_metadata_queries_and_preserves_structural_details():
    connection = _Connection()

    async def connect(**kwargs):
        assert kwargs["database"] == "analytics"
        assert kwargs["user"] == "reader"
        assert kwargs["password"] == "synthetic-password"
        return connection

    adapter = PostgresOnboardingAdapter(
        settings=_settings(),
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )
    result = await adapter.discover_selected_tables(
        host="client-postgres.stage3b.test",
        port=5432,
        database_name="analytics",
        username="reader",
        password="synthetic-password",
        custom_ca_pem=None,
        selected_tables=(InventoryTable("sales", "customers"), InventoryTable("sales", "orders")),
    )

    assert connection.closed is True
    assert [(table.schema_name, table.table_name) for table in result] == [
        ("sales", "customers"),
        ("sales", "orders"),
    ]
    customers = result[0]
    assert customers.table_comment == "Customer table"
    assert customers.columns[1].column_comment == "Customer identifier"
    assert customers.columns[1].formatted_native_type == "integer"
    assert customers.columns[1].native_type_name == "int4"
    assert customers.declared_keys[0].column_names == ("tenant_id", "customer_id")
    orders = result[1]
    assert [column.ordinal_position for column in orders.columns] == [1, 2, 3]
    assert orders.foreign_keys[0].column_mappings[1].referenced_column_name == "customer_id"
    excluded = orders.foreign_keys[1]
    assert excluded.target_in_scope is False
    assert excluded.referenced_schema_name is None
    assert excluded.column_mappings == ()

    assert len(connection.calls) == 5
    for sql, parameters in connection.calls:
        assert "unnest($1::text[], $2::text[])" in sql
        assert parameters[0] == ["sales", "sales"]
        assert parameters[1] == ["customers", "orders"]
        assert "SELECT * FROM sales" not in sql
        assert "FROM sales." not in sql


async def test_discovery_fails_closed_when_selected_table_is_not_eligible():
    connection = _Connection(eligible=[{"schema_name": "sales", "table_name": "orders"}])

    async def connect(**_kwargs):
        return connection

    adapter = PostgresOnboardingAdapter(
        settings=_settings(),
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )
    with pytest.raises(SelectedTableUnavailableError):
        await adapter.discover_selected_tables(
            host="client-postgres.stage3b.test",
            port=5432,
            database_name="analytics",
            username="reader",
            password="synthetic-password",
            custom_ca_pem=None,
            selected_tables=(InventoryTable("sales", "customers"), InventoryTable("sales", "orders")),
        )
    assert connection.closed is True


async def test_discovery_rejects_metadata_limit_without_publishing_partial_structure():
    connection = _Connection()

    async def connect(**_kwargs):
        return connection

    adapter = PostgresOnboardingAdapter(
        settings=_settings(catalog_discovery_max_columns=1),
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )
    with pytest.raises(MetadataLimitError) as error:
        await adapter.discover_selected_tables(
            host="client-postgres.stage3b.test",
            port=5432,
            database_name="analytics",
            username="reader",
            password="synthetic-password",
            custom_ca_pem=None,
            selected_tables=(InventoryTable("sales", "customers"), InventoryTable("sales", "orders")),
        )
    assert error.value.code == "metadata_column_limit_exceeded"
    assert connection.closed is True


async def test_discovery_timeout_closes_the_client_connection():
    class _SlowConnection(_Connection):
        async def fetch(self, sql, *parameters):
            await asyncio.sleep(0.05)
            return await super().fetch(sql, *parameters)

    connection = _SlowConnection()

    async def connect(**_kwargs):
        return connection

    adapter = PostgresOnboardingAdapter(
        settings=_settings(catalog_discovery_timeout_seconds=0.01),
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )
    with pytest.raises(MetadataDiscoveryTimeoutError):
        await adapter.discover_selected_tables(
            host="client-postgres.stage3b.test",
            port=5432,
            database_name="analytics",
            username="reader",
            password="synthetic-password",
            custom_ca_pem=None,
            selected_tables=(InventoryTable("sales", "customers"), InventoryTable("sales", "orders")),
        )
    assert connection.closed is True
