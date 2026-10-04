"""Add saved-source maintenance and optional durable catalog scheduling.

Revision ID: 0015_stage7_source_operations
Revises: 0014_optional_source_profiling
Create Date: 2026-10-04

The migration stores only scheduling/lifecycle metadata.  It never migrates
credentials, connection strings, catalog rows, query results, or profile data.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015_stage7_source_operations"
down_revision: Union[str, None] = "0014_optional_source_profiling"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _source_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("configuration_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("catalog_refresh_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("catalog_refresh_interval_minutes", sa.Integer(), nullable=True),
        sa.Column("catalog_refresh_next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("catalog_last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("catalog_last_failure_code", sa.String(length=64), nullable=True),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        for column in _source_columns():
            op.add_column("data_sources", column)
        op.create_check_constraint(
            "configuration_generation_nonnegative",
            "data_sources",
            "configuration_generation >= 0",
        )
    else:
        # SQLite needs a table rebuild to add a check constraint; batch mode
        # preserves its existing foreign keys and test fixtures.
        with op.batch_alter_table("data_sources", recreate="always") as batch_op:
            for column in _source_columns():
                batch_op.add_column(column)
            batch_op.create_check_constraint(
                "configuration_generation_nonnegative", "configuration_generation >= 0"
            )

    op.add_column(
        "catalog_sync_runs",
        sa.Column("source_configuration_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "catalog_sync_runs",
        sa.Column("scheduled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_catalog_sync_runs_scheduled_status_requested",
        "catalog_sync_runs",
        ["scheduled", "status", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_catalog_sync_runs_scheduled_status_requested", table_name="catalog_sync_runs")
    op.drop_column("catalog_sync_runs", "scheduled")
    op.drop_column("catalog_sync_runs", "source_configuration_generation")
    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        op.drop_constraint("configuration_generation_nonnegative", "data_sources", type_="check")
        for name in (
            "catalog_last_failure_code",
            "catalog_last_success_at",
            "catalog_refresh_next_due_at",
            "catalog_refresh_interval_minutes",
            "catalog_refresh_enabled",
            "configuration_generation",
        ):
            op.drop_column("data_sources", name)
    else:
        with op.batch_alter_table("data_sources", recreate="always") as batch_op:
            batch_op.drop_constraint("configuration_generation_nonnegative", type_="check")
            for name in (
                "catalog_last_failure_code",
                "catalog_last_success_at",
                "catalog_refresh_next_due_at",
                "catalog_refresh_interval_minutes",
                "catalog_refresh_enabled",
                "configuration_generation",
            ):
                batch_op.drop_column(name)
