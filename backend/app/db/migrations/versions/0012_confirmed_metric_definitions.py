"""Persist owner-confirmed, immutable single-table metric definitions.

Revision ID: 0012_confirmed_metric_definitions
Revises: 0011_postgres_chat_bindings
Create Date: 2026-10-03

The definition payload is canonical typed configuration only. It contains no
SQL, formula language, credentials, customer rows, or copied catalog snapshot.
Older versions remain immutable so an execution can retain its confirmed
semantic provenance even after a newer version is saved or the metric retires.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0012_confirmed_metric_definitions"
down_revision: Union[str, None] = "0011_postgres_chat_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LIFECYCLE_ENUM = "metric_lifecycle_state_enum"
AGGREGATE_ENUM = "metric_aggregate_operation_enum"
RUN_METRIC_VERSION_FK = "fk_postgres_query_runs_metric_definition_version"
RUN_METRIC_VERSION_SOURCE_FK = "fk_postgres_query_runs_metric_definition_version_source"
RUN_METRIC_VERSION_INDEX = "ix_postgres_query_runs_metric_definition_version_id"


def _types() -> tuple[sa.Enum, sa.Enum]:
    return (
        sa.Enum("ACTIVE", "NEEDS_REVIEW", "RETIRED", name=LIFECYCLE_ENUM),
        sa.Enum("COUNT", "SUM", "AVERAGE", "MINIMUM", "MAXIMUM", name=AGGREGATE_ENUM),
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
    lifecycle, aggregate = _types()

    op.create_table(
        "metric_definitions",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("owner_user_id", uuid_type, nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=128), nullable=False),
        sa.Column("lifecycle_state", lifecycle, nullable=False, server_default="ACTIVE"),
        sa.Column("current_version_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("needs_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("needs_review_reason", sa.String(length=64), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", uuid_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("current_version_number > 0", name="current_version_positive"),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_metric_definitions_source_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_user_id"], ["users.id"],
            name="fk_metric_definitions_retired_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("source_id", "id", name="uq_metric_definitions_source_id_id"),
    )
    op.create_index("ix_metric_definitions_source_id", "metric_definitions", ["source_id"])
    op.create_index("ix_metric_definitions_owner_user_id", "metric_definitions", ["owner_user_id"])
    op.create_index(
        "uq_metric_definitions_source_active_normalized_name",
        "metric_definitions",
        ["source_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'ACTIVE'"),
        sqlite_where=sa.text("lifecycle_state = 'ACTIVE'"),
    )

    op.create_table(
        "metric_definition_versions",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("metric_definition_id", uuid_type, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("confirmed_snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("aggregate_operation", aggregate, nullable=False),
        sa.Column("aggregate_column_id", uuid_type, nullable=True),
        sa.Column("unit_label", sa.String(length=64), nullable=True),
        sa.Column("default_date_column_id", uuid_type, nullable=True),
        sa.Column("default_time_zone", sa.String(length=128), nullable=True),
        sa.Column("definition_json", json_type, nullable=False),
        sa.Column("confirmed_by_user_id", uuid_type, nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version_number > 0", name="version_positive"),
        sa.CheckConstraint(
            "aggregate_operation = 'COUNT' OR aggregate_column_id IS NOT NULL",
            name="aggregate_column_matches_operation",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "metric_definition_id"],
            ["metric_definitions.source_id", "metric_definitions.id"],
            name="fk_metric_definition_versions_definition_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "confirmed_snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_metric_definition_versions_catalog_asset",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "confirmed_by_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_metric_definition_versions_confirming_owner",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("metric_definition_id", "version_number", name="uq_metric_definition_versions_number"),
        sa.UniqueConstraint("source_id", "id", name="uq_metric_definition_versions_source_id_id"),
    )
    op.create_index("ix_metric_definition_versions_source_id", "metric_definition_versions", ["source_id"])
    op.create_index("ix_metric_definition_versions_metric_definition_id", "metric_definition_versions", ["metric_definition_id"])

    if is_postgres:
        op.add_column(
            "postgres_query_runs",
            sa.Column("metric_definition_version_id", uuid_type, nullable=True),
        )
        op.create_foreign_key(
            RUN_METRIC_VERSION_FK,
            "postgres_query_runs",
            "metric_definition_versions",
            ["metric_definition_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            RUN_METRIC_VERSION_SOURCE_FK,
            "postgres_query_runs",
            "metric_definition_versions",
            ["source_id", "metric_definition_version_id"],
            ["source_id", "id"],
            # NO ACTION is intentional: a composite SET NULL would also null
            # non-null source_id. The simple FK above clears only the nullable
            # version leg if a version is ever deleted.
        )
        op.create_index(
            RUN_METRIC_VERSION_INDEX,
            "postgres_query_runs",
            ["metric_definition_version_id"],
        )
    else:
        # SQLite needs a recreate to add a foreign key to an existing table.
        with op.batch_alter_table("postgres_query_runs", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("metric_definition_version_id", uuid_type, nullable=True))
            batch_op.create_foreign_key(
                RUN_METRIC_VERSION_FK,
                "metric_definition_versions",
                ["metric_definition_version_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_foreign_key(
                RUN_METRIC_VERSION_SOURCE_FK,
                "metric_definition_versions",
                ["source_id", "metric_definition_version_id"],
                ["source_id", "id"],
            )
            batch_op.create_index(RUN_METRIC_VERSION_INDEX, ["metric_definition_version_id"])


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"

    if is_postgres:
        op.drop_index(RUN_METRIC_VERSION_INDEX, table_name="postgres_query_runs")
        op.drop_constraint(RUN_METRIC_VERSION_SOURCE_FK, "postgres_query_runs", type_="foreignkey")
        op.drop_constraint(RUN_METRIC_VERSION_FK, "postgres_query_runs", type_="foreignkey")
        op.drop_column("postgres_query_runs", "metric_definition_version_id")
    else:
        with op.batch_alter_table("postgres_query_runs", recreate="always") as batch_op:
            batch_op.drop_index(RUN_METRIC_VERSION_INDEX)
            batch_op.drop_constraint(RUN_METRIC_VERSION_SOURCE_FK, type_="foreignkey")
            batch_op.drop_constraint(RUN_METRIC_VERSION_FK, type_="foreignkey")
            batch_op.drop_column("metric_definition_version_id")

    op.drop_index("ix_metric_definition_versions_metric_definition_id", table_name="metric_definition_versions")
    op.drop_index("ix_metric_definition_versions_source_id", table_name="metric_definition_versions")
    op.drop_table("metric_definition_versions")
    op.drop_index("uq_metric_definitions_source_active_normalized_name", table_name="metric_definitions")
    op.drop_index("ix_metric_definitions_owner_user_id", table_name="metric_definitions")
    op.drop_index("ix_metric_definitions_source_id", table_name="metric_definitions")
    op.drop_table("metric_definitions")
    if is_postgres:
        postgresql.ENUM(name=AGGREGATE_ENUM).drop(bind, checkfirst=True)
        postgresql.ENUM(name=LIFECYCLE_ENUM).drop(bind, checkfirst=True)
