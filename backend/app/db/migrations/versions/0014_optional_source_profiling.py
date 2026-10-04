"""Add durable aggregate-only optional PostgreSQL profile records.

Revision ID: 0014_optional_source_profiling
Revises: 0013_confirmed_relationship_definitions
Create Date: 2026-10-03

No sampled rows, source values, SQL text, credentials, or driver messages are
stored here.  The schema persists only bounded-run state, catalog provenance,
aggregate observations, and deterministic sample-qualified findings.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0014_optional_source_profiling"
down_revision: Union[str, None] = "0013_confirmed_relationship_definitions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RUN_STATUS_ENUM = "source_profile_run_status_enum"
SAMPLING_ENUM = "source_profile_sampling_method_enum"
OBSERVATION_SAMPLING_ENUM = "source_profile_observation_sampling_method_enum"
POPULATION_ENUM = "source_profile_population_enum"
COLUMN_POPULATION_ENUM = "source_profile_column_population_enum"
OBSERVATION_STATUS_ENUM = "source_profile_observation_status_enum"
COLUMN_OBSERVATION_STATUS_ENUM = "source_profile_column_observation_status_enum"
FINDING_TYPE_ENUM = "source_profile_finding_type_enum"
FINDING_SEVERITY_ENUM = "source_profile_finding_severity_enum"
PROFILE_GENERATION_CHECK = "ck_data_sources_profiling_generation_nonnegative"


def _types() -> tuple[sa.Enum, ...]:
    return (
        sa.Enum("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name=RUN_STATUS_ENUM),
        sa.Enum("SYSTEM_TABLE_SAMPLE", name=SAMPLING_ENUM),
        sa.Enum("SYSTEM_TABLE_SAMPLE", name=OBSERVATION_SAMPLING_ENUM),
        sa.Enum("OBSERVED_SAMPLE", name=POPULATION_ENUM),
        sa.Enum("OBSERVED_SAMPLE", name=COLUMN_POPULATION_ENUM),
        sa.Enum("COMPLETE", "EMPTY_SAMPLE", "UNSUPPORTED", "INCOMPLETE", name=OBSERVATION_STATUS_ENUM),
        sa.Enum("COMPLETE", "EMPTY_SAMPLE", "UNSUPPORTED", "INCOMPLETE", name=COLUMN_OBSERVATION_STATUS_ENUM),
        sa.Enum("OBSERVED_NULL_RATE", "OBSERVED_CONSTANT_NON_NULL", name=FINDING_TYPE_ENUM),
        sa.Enum("LOW", "MEDIUM", name=FINDING_SEVERITY_ENUM),
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    uuid_default = (
        sa.text("uuid_generate_v4()")
        if is_postgres
        else sa.text("(lower(hex(randomblob(16))))")
    )
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()
    (
        run_status,
        sampling,
        observation_sampling,
        population,
        column_population,
        observation_status,
        column_observation_status,
        finding_type,
        finding_severity,
    ) = _types()

    # A config-generation fence makes a disable/configuration change visible
    # to a worker that claimed a run before the change.
    if is_postgres:
        op.add_column(
            "data_sources",
            sa.Column(
                "profiling_generation", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
        )
        op.create_check_constraint(
            "profiling_generation_nonnegative",
            "data_sources",
            "profiling_generation >= 0",
        )
    else:
        with op.batch_alter_table("data_sources", recreate="always") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "profiling_generation", sa.Integer(), nullable=False, server_default=sa.text("0")
                )
            )
            batch_op.create_check_constraint(
                "profiling_generation_nonnegative", "profiling_generation >= 0"
            )

    op.create_table(
        "source_profile_runs",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("requesting_user_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("selection_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("scope_count", sa.Integer(), nullable=False),
        sa.Column("selected_asset_count", sa.Integer(), nullable=False),
        sa.Column("selected_column_count", sa.Integer(), nullable=False),
        sa.Column("config_generation", sa.Integer(), nullable=False),
        sa.Column("sampling_method", sampling, nullable=False),
        sa.Column("sample_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("sample_limit", sa.Integer(), nullable=False),
        sa.Column("sample_seed", sa.Integer(), nullable=False),
        sa.Column("max_operations", sa.Integer(), nullable=False),
        sa.Column("status", run_status, nullable=False, server_default="QUEUED"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", uuid_type, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("scope_count > 0", name="scope_count_positive"),
        sa.CheckConstraint("selected_asset_count > 0", name="selected_asset_count_positive"),
        sa.CheckConstraint("selected_column_count >= 0", name="selected_column_count_nonnegative"),
        sa.CheckConstraint("sample_percent > 0 AND sample_percent <= 10", name="sample_percent_bounded"),
        sa.CheckConstraint("sample_limit > 0", name="sample_limit_positive"),
        sa.CheckConstraint("sample_seed >= 0", name="sample_seed_nonnegative"),
        sa.CheckConstraint("max_operations > 0", name="max_operations_positive"),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint("config_generation >= 0", name="config_generation_nonnegative"),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_fields_match",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "requesting_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_source_profile_runs_source_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_source_profile_runs_source_snapshot",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "id", name="uq_source_profile_runs_source_id_id"),
        sa.UniqueConstraint(
            "source_id", "id", "snapshot_id", name="uq_source_profile_runs_source_id_id_snapshot_id"
        ),
    )
    op.create_index("ix_source_profile_runs_source_id", "source_profile_runs", ["source_id"])
    op.create_index(
        "ix_source_profile_runs_source_status_requested",
        "source_profile_runs",
        ["source_id", "status", "requested_at"],
    )

    op.create_table(
        "source_profile_run_assets",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("selected_column_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("selected_column_count >= 0", name="selected_column_count_nonnegative"),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id"],
            [
                "source_profile_runs.source_id",
                "source_profile_runs.id",
                "source_profile_runs.snapshot_id",
            ],
            name="fk_source_profile_run_assets_run_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_source_profile_run_assets_catalog_asset",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "run_id", "asset_id", name="uq_source_profile_run_assets_run_asset"),
        sa.UniqueConstraint(
            "source_id", "run_id", "snapshot_id", "asset_id", name="uq_source_profile_run_assets_full"
        ),
    )
    op.create_index("ix_source_profile_run_assets_run_id", "source_profile_run_assets", ["run_id"])

    op.create_table(
        "source_profile_run_columns",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("column_id", uuid_type, nullable=False),
        sa.Column("scalar_kind", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id"],
            [
                "source_profile_run_assets.source_id",
                "source_profile_run_assets.run_id",
                "source_profile_run_assets.snapshot_id",
                "source_profile_run_assets.asset_id",
            ],
            name="fk_source_profile_run_columns_run_asset",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id", "column_id"],
            [
                "catalog_columns.source_id",
                "catalog_columns.snapshot_id",
                "catalog_columns.asset_id",
                "catalog_columns.id",
            ],
            name="fk_source_profile_run_columns_catalog_column",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "run_id", "column_id", name="uq_source_profile_run_columns_run_column"),
        sa.UniqueConstraint(
            "source_id", "run_id", "snapshot_id", "asset_id", "column_id",
            name="uq_source_profile_run_columns_full",
        ),
    )
    op.create_index("ix_source_profile_run_columns_run_id", "source_profile_run_columns", ["run_id"])

    op.create_table(
        "source_profile_table_observations",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sampling_method", observation_sampling, nullable=False),
        sa.Column("sample_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("sample_limit", sa.Integer(), nullable=False),
        sa.Column("sample_seed", sa.Integer(), nullable=False),
        sa.Column("population", population, nullable=False, server_default="OBSERVED_SAMPLE"),
        sa.Column("observed_row_count", sa.Integer(), nullable=False),
        sa.Column("status", observation_status, nullable=False),
        sa.Column("warning_codes", json_type, nullable=False),
        sa.CheckConstraint("observed_row_count >= 0", name="observed_row_count_nonnegative"),
        sa.CheckConstraint("sample_percent > 0 AND sample_percent <= 10", name="sample_percent_bounded"),
        sa.CheckConstraint("sample_limit > 0", name="sample_limit_positive"),
        sa.CheckConstraint("sample_seed >= 0", name="sample_seed_nonnegative"),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id"],
            [
                "source_profile_run_assets.source_id",
                "source_profile_run_assets.run_id",
                "source_profile_run_assets.snapshot_id",
                "source_profile_run_assets.asset_id",
            ],
            name="fk_source_profile_table_observations_run_asset",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "run_id", "asset_id", name="uq_source_profile_table_observations_run_asset"),
        sa.UniqueConstraint("source_id", "id", name="uq_source_profile_table_observations_source_id_id"),
        sa.UniqueConstraint(
            "source_id",
            "run_id",
            "id",
            name="uq_source_profile_table_observations_source_run_id",
        ),
        sa.UniqueConstraint(
            "source_id",
            "run_id",
            "snapshot_id",
            "asset_id",
            "id",
            name="uq_source_profile_table_observations_provenance",
        ),
    )
    op.create_index("ix_source_profile_table_observations_run_id", "source_profile_table_observations", ["run_id"])

    op.create_table(
        "source_profile_column_observations",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("column_id", uuid_type, nullable=False),
        sa.Column("table_observation_id", uuid_type, nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("population", column_population, nullable=False, server_default="OBSERVED_SAMPLE"),
        sa.Column("observed_row_count", sa.Integer(), nullable=False),
        sa.Column("null_count", sa.Integer(), nullable=False),
        sa.Column("non_null_count", sa.Integer(), nullable=False),
        sa.Column("null_rate", sa.Numeric(9, 8), nullable=True),
        sa.Column("distinct_count", sa.Integer(), nullable=False),
        sa.Column("numeric_count", sa.Integer(), nullable=True),
        sa.Column("numeric_sum", sa.Numeric(38, 10), nullable=True),
        sa.Column("numeric_minimum", sa.Numeric(38, 10), nullable=True),
        sa.Column("numeric_maximum", sa.Numeric(38, 10), nullable=True),
        sa.Column("numeric_mean", sa.Numeric(38, 10), nullable=True),
        sa.Column("status", column_observation_status, nullable=False),
        sa.Column("warning_codes", json_type, nullable=False),
        sa.CheckConstraint("observed_row_count >= 0", name="observed_row_count_nonnegative"),
        sa.CheckConstraint("null_count >= 0", name="null_count_nonnegative"),
        sa.CheckConstraint("non_null_count >= 0", name="non_null_count_nonnegative"),
        sa.CheckConstraint("distinct_count >= 0", name="distinct_count_nonnegative"),
        sa.CheckConstraint("distinct_count <= non_null_count", name="distinct_count_within_non_null"),
        sa.CheckConstraint("null_count + non_null_count = observed_row_count", name="counts_match_population"),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id", "column_id"],
            [
                "source_profile_run_columns.source_id",
                "source_profile_run_columns.run_id",
                "source_profile_run_columns.snapshot_id",
                "source_profile_run_columns.asset_id",
                "source_profile_run_columns.column_id",
            ],
            name="fk_source_profile_column_observations_run_column",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id", "table_observation_id"],
            [
                "source_profile_table_observations.source_id",
                "source_profile_table_observations.run_id",
                "source_profile_table_observations.snapshot_id",
                "source_profile_table_observations.asset_id",
                "source_profile_table_observations.id",
            ],
            name="fk_source_profile_column_observations_table_provenance",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "run_id", "column_id", name="uq_source_profile_column_observations_run_column"),
        sa.UniqueConstraint("source_id", "id", name="uq_source_profile_column_observations_source_id_id"),
        sa.UniqueConstraint(
            "source_id",
            "run_id",
            "table_observation_id",
            "id",
            name="uq_source_profile_column_observations_finding_provenance",
        ),
    )
    op.create_index("ix_source_profile_column_observations_run_id", "source_profile_column_observations", ["run_id"])

    op.create_table(
        "source_profile_findings",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("table_observation_id", uuid_type, nullable=False),
        sa.Column("column_observation_id", uuid_type, nullable=False),
        sa.Column("finding_type", finding_type, nullable=False),
        sa.Column("severity", finding_severity, nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("limitations", sa.String(length=500), nullable=False),
        sa.Column("evidence_json", json_type, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id"],
            ["source_profile_runs.source_id", "source_profile_runs.id"],
            name="fk_source_profile_findings_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id", "table_observation_id"],
            [
                "source_profile_table_observations.source_id",
                "source_profile_table_observations.run_id",
                "source_profile_table_observations.id",
            ],
            name="fk_source_profile_findings_table_provenance",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id", "table_observation_id", "column_observation_id"],
            [
                "source_profile_column_observations.source_id",
                "source_profile_column_observations.run_id",
                "source_profile_column_observations.table_observation_id",
                "source_profile_column_observations.id",
            ],
            name="fk_source_profile_findings_column_provenance",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_id", "run_id", "finding_type", "column_observation_id",
            name="uq_source_profile_findings_run_type_column",
        ),
    )
    op.create_index("ix_source_profile_findings_source_run", "source_profile_findings", ["source_id", "run_id"])
    op.create_index("ix_source_profile_findings_run_id", "source_profile_findings", ["run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"

    op.drop_index("ix_source_profile_findings_run_id", table_name="source_profile_findings")
    op.drop_index("ix_source_profile_findings_source_run", table_name="source_profile_findings")
    op.drop_table("source_profile_findings")
    op.drop_index("ix_source_profile_column_observations_run_id", table_name="source_profile_column_observations")
    op.drop_table("source_profile_column_observations")
    op.drop_index("ix_source_profile_table_observations_run_id", table_name="source_profile_table_observations")
    op.drop_table("source_profile_table_observations")
    op.drop_index("ix_source_profile_run_columns_run_id", table_name="source_profile_run_columns")
    op.drop_table("source_profile_run_columns")
    op.drop_index("ix_source_profile_run_assets_run_id", table_name="source_profile_run_assets")
    op.drop_table("source_profile_run_assets")
    op.drop_index("ix_source_profile_runs_source_status_requested", table_name="source_profile_runs")
    op.drop_index("ix_source_profile_runs_source_id", table_name="source_profile_runs")
    op.drop_table("source_profile_runs")

    if is_postgres:
        op.drop_constraint(op.f(PROFILE_GENERATION_CHECK), "data_sources", type_="check")
        op.drop_column("data_sources", "profiling_generation")
    else:
        with op.batch_alter_table("data_sources", recreate="always") as batch_op:
            batch_op.drop_constraint(op.f(PROFILE_GENERATION_CHECK), type_="check")
            batch_op.drop_column("profiling_generation")

    if is_postgres:
        for enum_name in (
            FINDING_SEVERITY_ENUM,
            FINDING_TYPE_ENUM,
            COLUMN_OBSERVATION_STATUS_ENUM,
            OBSERVATION_STATUS_ENUM,
            COLUMN_POPULATION_ENUM,
            POPULATION_ENUM,
            OBSERVATION_SAMPLING_ENUM,
            SAMPLING_ENUM,
            RUN_STATUS_ENUM,
        ):
            postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)
