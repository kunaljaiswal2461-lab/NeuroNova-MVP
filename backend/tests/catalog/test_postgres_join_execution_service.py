"""Focused Stage 5B gateway pin tests without pretending to run PostgreSQL.

The adapter suite covers two-relation lock/key checks; this seam test proves
that a relationship changed after remote completion suppresses its result
before the executor returns it to a caller. It intentionally stubs the
already-tested catalog/transport boundaries rather than claiming a fake is a
live database proof.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.postgres_query_compiler as compiler_module
import app.services.postgres_query_executor as executor_module
from app.connectors.postgres_query_executor import (
    ActualPostgresOutputColumn,
    LiveQueryExpectation,
    RemotePostgresExecution,
)
from app.core.config import Settings
from app.db.models.postgres_query_run import PostgresQueryRun
from app.services.postgres_query_compiler import (
    CompiledPostgresJoin,
    CompiledPostgresQuery,
    ExpectedPostgresOutputColumn,
    PostgresCalculationSemantics,
)
from app.services.postgres_query_executor import execute_postgres_query
from app.services.postgres_query_plan import PostgresQueryPlan


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage5b-join-executor-test-key",
        database_url="sqlite+aiosqlite://",
    )


def _compiled_and_plan() -> tuple[CompiledPostgresQuery, PostgresQueryPlan]:
    base_asset_id = uuid4()
    lookup_asset_id = uuid4()
    relationship_id = uuid4()
    relationship_version_id = uuid4()
    projection_column_id = uuid4()
    compiled = CompiledPostgresQuery(
        sql='SELECT "b"."order_id" AS "__nv_p0"',
        parameters=(),
        source_id=uuid4(),
        snapshot_id=uuid4(),
        asset_id=base_asset_id,
        expected_output_columns=(
            ExpectedPostgresOutputColumn(
                alias="__nv_p0",
                logical_type="integer",
                postgres_type="int8",
                source_column_id=projection_column_id,
                source_column_name="order_id",
                source_asset_id=base_asset_id,
            ),
        ),
        effective_limit=1,
        ordering=(),
        semantics=PostgresCalculationSemantics(
            filters_apply_before_aggregation=True,
            post_aggregation_filters_supported=False,
            count_all_is_row_count=False,
            count_column_excludes_nulls=False,
            result_order="unordered_bounded",
            timestamp_bucket_notes=(),
            join_type="left",
            left_join_preserves_unmatched_base_rows=True,
        ),
        fingerprint="b" * 64,
        join=CompiledPostgresJoin(
            relationship_id=relationship_id,
            relationship_version_id=relationship_version_id,
            version_number=1,
            join_type="left",
            lookup_asset_id=lookup_asset_id,
            lookup_schema_name="sales",
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
    plan = PostgresQueryPlan.model_validate(
        {
            "asset_id": base_asset_id,
            "join": {
                "relationship_id": relationship_id,
                "relationship_version_id": relationship_version_id,
                "lookup_asset_id": lookup_asset_id,
                "join_type": "left",
            },
            "projections": (
                {"asset_id": base_asset_id, "column_id": projection_column_id},
            ),
            "limit": 1,
        }
    )
    return compiled, plan


def test_relationship_run_provenance_foreign_keys_are_source_scoped():
    nullable_reference = next(
        item
        for item in PostgresQueryRun.__table__.foreign_key_constraints
        if item.name == "fk_postgres_query_runs_relationship_definition_version"
    )
    assert [column.name for column in nullable_reference.columns] == [
        "relationship_definition_version_id"
    ]
    assert [element.target_fullname for element in nullable_reference.elements] == [
        "relationship_definition_versions.id"
    ]
    assert nullable_reference.elements[0].ondelete == "SET NULL"
    source_reference = next(
        item
        for item in PostgresQueryRun.__table__.foreign_key_constraints
        if item.name == "fk_postgres_query_runs_relationship_definition_version_source"
    )
    assert [column.name for column in source_reference.columns] == [
        "source_id",
        "relationship_definition_version_id",
    ]
    assert [element.target_fullname for element in source_reference.elements] == [
        "relationship_definition_versions.source_id",
        "relationship_definition_versions.id",
    ]


async def test_changed_relationship_after_remote_read_is_withheld_at_executor_boundary(monkeypatch):
    compiled, plan = _compiled_and_plan()
    expectation = LiveQueryExpectation(
        schema_name="sales",
        table_name="orders",
        columns=(),
        expected_output_columns=compiled.expected_output_columns,
    )
    source = SimpleNamespace(
        host="warehouse.example.test",
        port=5432,
        database_name="analytics",
        username="reader",
        password="secret",
        custom_ca_pem=None,
    )
    relationship_pin_checks = 0
    source_version_checks = 0
    run_arguments: dict[str, object] = {}

    async def fake_authorize(*_args, **_kwargs):
        return compiled, expectation

    async def fake_load_source(*_args, **_kwargs):
        return source

    async def fake_delivery_access(*_args, **_kwargs):
        return None

    async def fake_create_run(*_args, **kwargs):
        run_arguments.update(kwargs)

    async def fake_finalize_run(*_args, **_kwargs):
        return None

    async def fake_source_version_check(*_args, **_kwargs):
        nonlocal source_version_checks
        source_version_checks += 1

    async def relationship_pin_verifier() -> bool:
        nonlocal relationship_pin_checks
        relationship_pin_checks += 1
        # Initial authorization and pre-gateway checks are valid; retirement
        # or a catalog incompatibility wins after remote work but before
        # delivery.
        return relationship_pin_checks < 3

    class _Adapter:
        calls = 0

        async def execute(self, **_kwargs):
            self.calls += 1
            return RemotePostgresExecution(
                actual_output_columns=(
                    ActualPostgresOutputColumn(
                        alias="__nv_p0",
                        postgres_type="int8",
                        postgres_type_schema="pg_catalog",
                    ),
                ),
                safety_row_truncated=False,
                collector_stopped=False,
            )

    adapter = _Adapter()
    monkeypatch.setattr(executor_module, "_authorize_and_resolve", fake_authorize)
    monkeypatch.setattr(executor_module, "_load_execution_source", fake_load_source)
    monkeypatch.setattr(executor_module, "_verify_delivery_source_access", fake_delivery_access)
    monkeypatch.setattr(executor_module, "_create_run", fake_create_run)
    monkeypatch.setattr(executor_module, "_finalize_run", fake_finalize_run)
    monkeypatch.setattr(
        executor_module,
        "_verify_relationship_version_source",
        fake_source_version_check,
    )

    result = await execute_postgres_query(
        lambda: None,
        owner_user_id=uuid4(),
        source_id=compiled.source_id,
        snapshot_id=compiled.snapshot_id,
        plan=plan,
        settings=_settings(),
        adapter_factory=lambda _settings: adapter,
        relationship_definition_version_id=compiled.join.relationship_version_id,
        relationship_pin_verifier=relationship_pin_verifier,
    )

    assert adapter.calls == 1
    assert relationship_pin_checks == 3
    # The post-remote pin failure short-circuits the redundant provenance
    # lookup, so only the pre-run source-local verification is needed.
    assert source_version_checks == 1
    assert run_arguments["relationship_definition_version_id"] == compiled.join.relationship_version_id
    assert result.status == "failed"
    assert result.failure_code == "relationship_definition_changed"
    assert result.rows == ()
