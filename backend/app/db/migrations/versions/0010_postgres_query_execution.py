"""Persist redacted provenance for controlled PostgreSQL query execution.

Revision ID: 0010_postgres_query_execution
Revises: 0009_catalog_discovery_worker
Create Date: 2026-10-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0010_postgres_query_execution"
down_revision: Union[str, None] = "0009_catalog_discovery_worker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RUN_STATUS_ENUM = "postgres_query_run_status_enum"


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    uuid_default = (
        sa.text("uuid_generate_v4()")
        if is_postgres
        else sa.text("(lower(hex(randomblob(16))))")
    )
    bool_default = sa.text("false") if is_postgres else sa.text("0")
    run_status = sa.Enum(
        "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name=RUN_STATUS_ENUM
    )

    op.create_table(
        "postgres_query_runs",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("owner_user_id", uuid_type, nullable=False),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", run_status, nullable=False, server_default="RUNNING"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("result_truncated", sa.Boolean(), nullable=False, server_default=bool_default),
        sa.Column("result_complete", sa.Boolean(), nullable=False, server_default=bool_default),
        sa.Column("returned_row_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("returned_row_count >= 0", name="returned_row_count_nonnegative"),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_postgres_query_runs_source_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_postgres_query_runs_source_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_postgres_query_runs_source_snapshot_asset",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_postgres_query_runs_owner_user_id", "postgres_query_runs", ["owner_user_id"])
    op.create_index("ix_postgres_query_runs_source_id", "postgres_query_runs", ["source_id"])
    op.create_index("ix_postgres_query_runs_snapshot_id", "postgres_query_runs", ["snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_postgres_query_runs_snapshot_id", table_name="postgres_query_runs")
    op.drop_index("ix_postgres_query_runs_source_id", table_name="postgres_query_runs")
    op.drop_index("ix_postgres_query_runs_owner_user_id", table_name="postgres_query_runs")
    op.drop_table("postgres_query_runs")
    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        postgresql.ENUM(name=RUN_STATUS_ENUM).drop(bind, checkfirst=True)
