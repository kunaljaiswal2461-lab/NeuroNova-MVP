"""Stage 5B remote-gateway checks for bounded two-relation execution.

These are controlled asyncpg-shaped transport tests. They establish lock
ordering and fail-closed structural expectations, not real PostgreSQL query
semantics; a disposable PostgreSQL case remains required for that layer.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import app.services.postgres_query_compiler as compiler_module
from app.connectors.connection_policy import PostgresConnectionPolicy
from app.connectors.postgres_query_executor import (
    LiveColumnExpectation,
    LiveForeignKeyExpectation,
    LiveRelationExpectation,
    LiveQueryExpectation,
    LiveUniqueKeyExpectation,
    PostgresExecutionSchemaChangedError,
    PostgresQueryExecutionAdapter,
)
from app.core.config import Settings
from app.services.postgres_query_compiler import (
    CompiledPostgresJoin,
    CompiledPostgresQuery,
    ExpectedPostgresOutputColumn,
    PostgresCalculationSemantics,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage5b-join-adapter-test-key",
        database_url="sqlite+aiosqlite://",
        postgres_execution_cursor_prefetch=1,
    )


def _relation(schema_name: str, table_name: str, oid: int) -> dict[str, Any]:
    return {
        "relation_oid": oid,
        "schema_name": schema_name,
        "table_name": table_name,
        "relation_kind": "r",
        "is_partition": False,
        "has_inheritance_children": False,
        "has_inheritance_membership": False,
        "row_security_enabled": False,
        "force_row_security": False,
        "role_is_superuser": False,
        "role_bypasses_rls": False,
        "role_is_table_owner": False,
        "has_schema_usage": True,
        "has_table_select": True,
    }


def _live_column(name: str, native_type_name: str) -> dict[str, Any]:
    oid = {"int8": 20, "text": 25}.get(native_type_name)
    return {
        "column_name": name,
        "native_type_schema": "pg_catalog",
        "native_type_name": native_type_name,
        "native_type_oid": oid,
        "native_type_modifier": -1,
        "is_nullable": False,
        "has_column_select": True,
    }


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Cursor:
    def __init__(self):
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return {"__nv_a0": 3}


class _Statement:
    def get_attributes(self):
        return [
            SimpleNamespace(
                name="__nv_a0",
                type=SimpleNamespace(name="int8", schema="pg_catalog"),
            )
        ]

    def cursor(self, *_parameters: Any, **_kwargs: Any):
        return _Cursor()


class _Connection:
    def __init__(
        self,
        *,
        unique_is_deferrable: bool = False,
        foreign_key_mapping_changed: bool = False,
    ):
        self.unique_is_deferrable = unique_is_deferrable
        self.foreign_key_mapping_changed = foreign_key_mapping_changed
        self.executed: list[str] = []
        self.prepared_sql: str | None = None
        self.closed = False
        self.terminated = False
        self.relations = {
            ("z", "orders"): _relation("z", "orders", 11),
            ("a", "customers"): _relation("a", "customers", 22),
        }
        self.columns = {
            11: {"customer_id": _live_column("customer_id", "int8")},
            22: {"customer_id": _live_column("customer_id", "int8")},
        }

    def transaction(self, **_kwargs: Any):
        return _Transaction()

    async def execute(self, sql: str):
        self.executed.append(sql)

    async def fetchrow(self, sql: str, *parameters: Any):
        if "transaction_read_only" in sql:
            return {
                "transaction_read_only": "on",
                "search_path": "pg_catalog",
                "time_zone": "UTC",
            }
        return dict(self.relations[(parameters[0], parameters[1])])

    async def fetch(self, sql: str, *parameters: Any):
        if "constraints.contype = 'f'" in sql:
            return [
                {
                    "constraint_name": "orders_customer_fkey",
                    "constraint_type": "f",
                    "is_deferrable": False,
                    "is_initially_deferred": False,
                    "is_validated": True,
                    "ordinal_position": 1,
                    "base_column_name": (
                        "replaced_customer_id"
                        if self.foreign_key_mapping_changed
                        else "customer_id"
                    ),
                    "lookup_column_name": "customer_id",
                }
            ]
        if "constraints.conname" in sql:
            return [
                {
                    "constraint_name": "customers_pkey",
                    "constraint_type": "p",
                    "is_deferrable": self.unique_is_deferrable,
                    "is_initially_deferred": False,
                    "is_validated": True,
                    "is_unique": True,
                    "is_valid": True,
                    "is_ready": True,
                    "is_partial": False,
                    "ordinal_position": 1,
                    "column_name": "customer_id",
                }
            ]
        oid, names = parameters
        return [dict(self.columns[oid][name]) for name in names]

    async def prepare(self, sql: str):
        self.prepared_sql = sql
        return _Statement()

    async def close(self):
        self.closed = True

    async def terminate(self):
        self.terminated = True


async def _resolver(_host: str, _port: int) -> list[str]:
    return ["127.0.0.1"]


def _compiled() -> CompiledPostgresQuery:
    base_asset_id, lookup_asset_id = uuid4(), uuid4()
    compiled = CompiledPostgresQuery(
        sql='SELECT pg_catalog.count(*) AS "__nv_a0"',
        parameters=(),
        source_id=uuid4(),
        snapshot_id=uuid4(),
        asset_id=base_asset_id,
        expected_output_columns=(
            ExpectedPostgresOutputColumn(
                alias="__nv_a0",
                logical_type="count",
                postgres_type="int8",
                source_column_id=None,
                source_column_name=None,
                operation="count_all",
            ),
        ),
        effective_limit=1,
        ordering=(),
        semantics=PostgresCalculationSemantics(
            filters_apply_before_aggregation=True,
            post_aggregation_filters_supported=False,
            count_all_is_row_count=True,
            count_column_excludes_nulls=False,
            result_order="unordered_bounded",
            timestamp_bucket_notes=(),
            join_type="left",
            left_join_preserves_unmatched_base_rows=True,
            lookup_filters_in_on=False,
        ),
        fingerprint="a" * 64,
        join=CompiledPostgresJoin(
            relationship_id=uuid4(),
            relationship_version_id=uuid4(),
            version_number=1,
            join_type="left",
            lookup_asset_id=lookup_asset_id,
            lookup_schema_name="a",
            lookup_table_name="customers",
            column_pairs=((uuid4(), uuid4()),),
            foreign_key_constraint_name="orders_customer_fkey",
            foreign_key_base_column_names=("customer_id",),
            foreign_key_lookup_column_names=("customer_id",),
            target_key_constraint_name="customers_pkey",
            target_key_kind="primary_key",
            target_key_column_names=("customer_id",),
        ),
    )
    object.__setattr__(compiled, "_seal", compiler_module._COMPILED_QUERY_SEAL)
    return compiled


def _expectation() -> LiveQueryExpectation:
    int8_column = LiveColumnExpectation(
        name="customer_id",
        native_type_schema="pg_catalog",
        native_type_name="int8",
        native_type_oid=20,
        native_type_modifier=-1,
        is_nullable=False,
    )
    return LiveQueryExpectation(
        schema_name="z",
        table_name="orders",
        columns=(int8_column,),
        expected_output_columns=_compiled().expected_output_columns,
        lookup_relation=LiveRelationExpectation(
            schema_name="a",
            table_name="customers",
            columns=(int8_column,),
            unique_key=LiveUniqueKeyExpectation(
                constraint_name="customers_pkey",
                key_kind="primary_key",
                column_names=("customer_id",),
            ),
        ),
        foreign_key=LiveForeignKeyExpectation(
            constraint_name="orders_customer_fkey",
            base_column_names=("customer_id",),
            lookup_column_names=("customer_id",),
        ),
    )


async def _execute(connection: _Connection):
    settings = _settings()

    async def connect(**_kwargs: Any):
        return connection

    adapter = PostgresQueryExecutionAdapter(
        settings=settings,
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )
    seen: list[tuple[Any, ...]] = []
    return await adapter.execute(
        host="warehouse.example.test",
        port=5432,
        database_name="analytics",
        username="reader",
        password="secret",
        custom_ca_pem=None,
        compiled=_compiled(),
        expectation=_expectation(),
        max_rows=10,
        row_consumer=lambda row: seen.append(row) or True,
    )


async def test_dual_relation_gateway_locks_in_canonical_order_and_rechecks_unique_target_key():
    connection = _Connection()
    result = await _execute(connection)

    lock_statements = [statement for statement in connection.executed if statement.startswith("LOCK TABLE")]
    assert lock_statements == [
        'LOCK TABLE "a"."customers" IN ACCESS SHARE MODE',
        'LOCK TABLE "z"."orders" IN ACCESS SHARE MODE',
    ]
    assert connection.prepared_sql is not None
    assert result.actual_output_columns[0].alias == "__nv_a0"
    assert connection.closed is True


async def test_dual_relation_gateway_rejects_deferrable_target_key_before_prepare():
    connection = _Connection(unique_is_deferrable=True)

    with pytest.raises(PostgresExecutionSchemaChangedError):
        await _execute(connection)

    assert connection.prepared_sql is None
    assert connection.terminated is True


async def test_dual_relation_gateway_rejects_changed_declared_fk_mapping_before_prepare():
    connection = _Connection(foreign_key_mapping_changed=True)

    with pytest.raises(PostgresExecutionSchemaChangedError):
        await _execute(connection)

    assert connection.prepared_sql is None
    assert connection.terminated is True


async def test_gateway_rejects_mismatched_join_expectation_before_connecting():
    compiled = _compiled()
    expectation = _expectation()
    assert expectation.lookup_relation is not None
    mismatched_expectation = replace(
        expectation,
        lookup_relation=replace(
            expectation.lookup_relation,
            unique_key=LiveUniqueKeyExpectation(
                constraint_name="wrong_constraint",
                key_kind="primary_key",
                column_names=("customer_id",),
            ),
        ),
    )
    connect_called = False

    async def connect(**_kwargs: Any):
        nonlocal connect_called
        connect_called = True
        raise AssertionError("mismatched join facts must fail before a remote connection")

    adapter = PostgresQueryExecutionAdapter(
        settings=_settings(),
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )
    with pytest.raises(PostgresExecutionSchemaChangedError):
        await adapter.execute(
            host="warehouse.example.test",
            port=5432,
            database_name="analytics",
            username="reader",
            password="secret",
            custom_ca_pem=None,
            compiled=compiled,
            expectation=mismatched_expectation,
            max_rows=10,
            row_consumer=lambda _row: True,
        )
    assert connect_called is False
