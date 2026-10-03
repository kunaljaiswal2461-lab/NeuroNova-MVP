"""Contract tests for the Stage 6A aggregate-only PostgreSQL profile adapter.

These tests use asyncpg-shaped fakes.  They prove the local fixed-operation,
TLS/session, cancellation, and aggregate-only boundary; a disposable
PostgreSQL run remains necessary to measure real SYSTEM-sampling cost and
timeout behaviour.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.connectors.connection_policy import PostgresConnectionPolicy
from app.connectors.postgres_profiling_adapter import (
    ProfileColumnStatus,
    ProfileExecutionBudget,
    ProfileOperation,
    ProfileRequest,
    ProfileSamplingConfig,
    ProfiledColumn,
    ProfiledRelation,
    PostgresProfilingAdapter,
    PostgresProfilingRequestError,
)
from app.connectors.postgres_query_executor import (
    PostgresExecutionCancellation,
    PostgresExecutionCancelledError,
    PostgresExecutionSchemaChangedError,
)
from app.core.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage6a-profiling-adapter-test-key",
        database_url="sqlite+aiosqlite://",
    )


async def _resolver(_host: str, _port: int) -> list[str]:
    return ["127.0.0.1"]


def _relation(*, oid: int = 41, table_name: str = "orders") -> dict[str, Any]:
    return {
        "relation_oid": oid,
        "schema_name": "sales",
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


def _column(name: str, native_type_name: str, *, nullable: bool) -> dict[str, Any]:
    type_oids = {"numeric": 1700, "text": 25, "jsonb": 3802}
    return {
        "column_name": name,
        "native_type_schema": "pg_catalog",
        "native_type_name": native_type_name,
        "native_type_oid": type_oids[native_type_name],
        "native_type_modifier": -1,
        "is_nullable": nullable,
        "has_column_select": True,
    }


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(
        self,
        *,
        aggregate_row: dict[str, Any] | None = None,
        relation_present: bool = True,
    ) -> None:
        self.aggregate_row = aggregate_row or {}
        self.relation_present = relation_present
        self.executed: list[str] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self.terminated = False

    def transaction(self, **kwargs: Any):
        assert kwargs == {"readonly": True, "isolation": "read_committed"}
        return _Transaction()

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def fetchrow(self, sql: str, *parameters: Any):
        self.fetchrow_calls.append((sql, parameters))
        if "transaction_read_only" in sql:
            return {
                "transaction_read_only": "on",
                "search_path": "pg_catalog",
                "time_zone": "UTC",
            }
        if "TABLESAMPLE SYSTEM" in sql:
            return dict(self.aggregate_row)
        if not self.relation_present:
            return None
        return _relation()

    async def fetch(self, sql: str, *parameters: Any):
        self.fetch_calls.append((sql, parameters))
        assert parameters[0] == 41
        names = parameters[1]
        all_columns = {
            "amount": _column("amount", "numeric", nullable=True),
            "status": _column("status", "text", nullable=False),
        }
        return [all_columns[name] for name in names]

    async def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


def _budget() -> ProfileExecutionBudget:
    return ProfileExecutionBudget(
        connect_timeout_seconds=1,
        statement_timeout_seconds=2,
        lock_timeout_seconds=1,
        idle_transaction_timeout_seconds=3,
        operation_timeout_seconds=5,
    )


def _profiled_column(
    column_id: UUID,
    name: str,
    type_name: str,
    *,
    nullable: bool,
) -> ProfiledColumn:
    type_oids = {"numeric": 1700, "text": 25, "jsonb": 3802}
    return ProfiledColumn(
        column_id=column_id,
        column_name=name,
        native_type_schema="pg_catalog",
        native_type_name=type_name,
        native_type_oid=type_oids[type_name],
        native_type_modifier=-1,
        is_nullable=nullable,
    )


def _request(*, columns: tuple[ProfiledColumn, ...]) -> ProfileRequest:
    return ProfileRequest(
        relation=ProfiledRelation(schema_name="sales", table_name="orders"),
        columns=columns,
        sampling=ProfileSamplingConfig(sample_percent=2.5, sample_limit=100, repeatable_seed=7),
        operations=(
            ProfileOperation.ROW_COUNT,
            ProfileOperation.COLUMN_NULL_DISTINCT,
            ProfileOperation.NUMERIC_SUMMARY,
        ),
        budget=_budget(),
    )


def _adapter(connection: _Connection, connect_calls: list[dict[str, Any]]) -> PostgresProfilingAdapter:
    async def connect(**kwargs: Any):
        connect_calls.append(kwargs)
        return connection

    return PostgresProfilingAdapter(
        settings=_settings(),
        policy=PostgresConnectionPolicy.for_disposable_test(_resolver),
        connect=connect,
    )


@pytest.mark.asyncio
async def test_profiles_one_shared_bounded_system_sample_and_returns_aggregate_only() -> None:
    amount_id = uuid4()
    status_id = uuid4()
    connection = _Connection(
        aggregate_row={
            "__nv_profile_observed_rows": 4,
            "__nv_profile_non_null_0": 2,
            "__nv_profile_distinct_0": 2,
            "__nv_profile_numeric_count_0": 2,
            "__nv_profile_sum_0": "12.5",
            "__nv_profile_min_0": "1.5",
            "__nv_profile_max_0": "11.0",
            "__nv_profile_mean_0": "6.25",
            "__nv_profile_non_null_1": 3,
            "__nv_profile_distinct_1": 2,
        }
    )
    connect_calls: list[dict[str, Any]] = []

    result = await _adapter(connection, connect_calls).execute(
        host="db.test",
        port=5432,
        database_name="warehouse",
        username="readonly",
        password="secret",
        custom_ca_pem=None,
        request=_request(
            columns=(
                _profiled_column(amount_id, "amount", "numeric", nullable=True),
                _profiled_column(status_id, "status", "text", nullable=False),
            )
        ),
    )

    assert result.observed_row_count == 4
    assert result.measure_coverage == "exact_for_observed_sample"
    assert result.sampling_method == "table_sample_system"
    assert result.sample_percent == 2.5
    assert result.sample_limit == 100
    amount, status = result.columns
    assert amount.status is ProfileColumnStatus.SUPPORTED
    assert amount.observed_null_count == 2
    assert amount.observed_distinct_count == 2
    assert amount.numeric_summary is not None
    assert amount.numeric_summary.sum_value == Decimal("12.5")
    assert amount.numeric_summary.minimum_value == Decimal("1.5")
    assert amount.numeric_summary.maximum_value == Decimal("11.0")
    assert amount.numeric_summary.mean_value == Decimal("6.25")
    assert status.observed_null_count == 1
    assert status.observed_distinct_count == 2
    assert status.numeric_summary is None

    aggregate_sql, aggregate_parameters = next(
        (sql, parameters)
        for sql, parameters in connection.fetchrow_calls
        if "TABLESAMPLE SYSTEM" in sql
    )
    assert "WITH __nv_profile_observed AS MATERIALIZED" in aggregate_sql
    assert "TABLESAMPLE SYSTEM ($1::real) REPEATABLE ($2::double precision)" in aggregate_sql
    assert "LIMIT $3::bigint" in aggregate_sql
    assert "COUNT(DISTINCT \"amount\")" in aggregate_sql
    assert "COUNT(DISTINCT \"status\")" in aggregate_sql
    assert aggregate_parameters == (2.5, 7.0, 100)
    assert not hasattr(result, "sql")
    assert "secret" not in repr(result)
    assert connection.closed is True
    assert connection.terminated is False
    assert connect_calls[0]["server_settings"]["default_transaction_read_only"] == "on"
    assert connect_calls[0]["server_settings"]["application_name"] == "neuronova-postgres-profiling"


@pytest.mark.asyncio
async def test_unsupported_scalar_column_is_not_selected_or_persisted_as_a_value() -> None:
    payload_id = uuid4()
    connection = _Connection(aggregate_row={"__nv_profile_observed_rows": 0})
    connect_calls: list[dict[str, Any]] = []

    result = await _adapter(connection, connect_calls).execute(
        host="db.test",
        port=5432,
        database_name="warehouse",
        username="readonly",
        password="secret",
        custom_ca_pem=None,
        request=_request(columns=(_profiled_column(payload_id, "payload", "jsonb", nullable=True),)),
    )

    assert result.observed_row_count == 0
    assert result.columns == (
        result.columns[0],
    )
    observation = result.columns[0]
    assert observation.status is ProfileColumnStatus.UNSUPPORTED_TYPE
    assert observation.warning_codes == ("unsupported_scalar_type",)
    assert observation.observed_null_count is None
    assert observation.observed_distinct_count is None
    aggregate_sql, _parameters = next(
        (sql, parameters)
        for sql, parameters in connection.fetchrow_calls
        if "TABLESAMPLE SYSTEM" in sql
    )
    assert '"payload"' not in aggregate_sql
    assert "1 AS __nv_profile_sample_marker" in aggregate_sql
    assert all("payload" not in str(call) for call in connection.fetch_calls)


def test_request_rejects_arbitrary_operation_and_full_table_sample_configuration() -> None:
    with pytest.raises(PostgresProfilingRequestError):
        ProfileRequest(
            relation=ProfiledRelation(schema_name="sales", table_name="orders"),
            columns=(),
            sampling=ProfileSamplingConfig(sample_percent=1, sample_limit=10, repeatable_seed=1),
            operations=(ProfileOperation.ROW_COUNT, "SELECT pg_sleep(1)"),  # type: ignore[arg-type]
            budget=_budget(),
        )
    with pytest.raises(PostgresProfilingRequestError):
        ProfileSamplingConfig(sample_percent=100, sample_limit=10, repeatable_seed=1)
    # SQLAlchemy returns Decimal for the durable run's NUMERIC sample percent.
    assert ProfileSamplingConfig(
        sample_percent=Decimal("2.5000"), sample_limit=10, repeatable_seed=1
    ).sample_percent == Decimal("2.5000")


@pytest.mark.asyncio
async def test_pre_cancelled_run_never_connects() -> None:
    connection = _Connection(aggregate_row={"__nv_profile_observed_rows": 0})
    connect_calls: list[dict[str, Any]] = []
    cancellation = PostgresExecutionCancellation()
    await cancellation.cancel()

    with pytest.raises(PostgresExecutionCancelledError):
        await _adapter(connection, connect_calls).execute(
            host="db.test",
            port=5432,
            database_name="warehouse",
            username="readonly",
            password="secret",
            custom_ca_pem=None,
            request=_request(columns=()),
            cancellation=cancellation,
        )
    assert connect_calls == []


@pytest.mark.asyncio
async def test_schema_disappearance_stops_before_aggregate_query() -> None:
    connection = _Connection(relation_present=False)
    connect_calls: list[dict[str, Any]] = []

    with pytest.raises(PostgresExecutionSchemaChangedError):
        await _adapter(connection, connect_calls).execute(
            host="db.test",
            port=5432,
            database_name="warehouse",
            username="readonly",
            password="secret",
            custom_ca_pem=None,
            request=_request(columns=()),
        )
    assert not any("TABLESAMPLE SYSTEM" in sql for sql, _parameters in connection.fetchrow_calls)
    assert connection.terminated is True


@pytest.mark.asyncio
async def test_non_finite_numeric_aggregate_is_explicitly_unavailable_without_retaining_value() -> None:
    amount_id = uuid4()
    connection = _Connection(
        aggregate_row={
            "__nv_profile_observed_rows": 1,
            "__nv_profile_non_null_0": 1,
            "__nv_profile_distinct_0": 1,
            "__nv_profile_numeric_count_0": 1,
            "__nv_profile_sum_0": "NaN",
            "__nv_profile_min_0": "1",
            "__nv_profile_max_0": "1",
            "__nv_profile_mean_0": "1",
        }
    )
    connect_calls: list[dict[str, Any]] = []

    result = await _adapter(connection, connect_calls).execute(
        host="db.test",
        port=5432,
        database_name="warehouse",
        username="readonly",
        password="secret",
        custom_ca_pem=None,
        request=_request(columns=(_profiled_column(amount_id, "amount", "numeric", nullable=True),)),
    )

    summary = result.columns[0].numeric_summary
    assert summary is not None
    assert summary.unavailable_reason == "non_finite_numeric_result"
    assert summary.sum_value is None
    assert "NaN" not in repr(asdict(result))
