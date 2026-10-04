"""Persist durable catalog-discovery worker leases and retries.

Revision ID: 0009_catalog_discovery_worker
Revises: 0008_catalog_persistence
Create Date: 2026-10-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0009_catalog_discovery_worker"
down_revision: Union[str, None] = "0008_catalog_persistence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)

    op.add_column(
        "catalog_sync_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("catalog_sync_runs", sa.Column("lease_owner", sa.String(length=128), nullable=True))
    op.add_column("catalog_sync_runs", sa.Column("lease_token", uuid_type, nullable=True))
    op.add_column(
        "catalog_sync_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "catalog_sync_runs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "catalog_sync_runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )

    constraints = (
        ("attempt_count_nonnegative", "attempt_count >= 0"),
        (
            "lease_fields_match",
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
        ),
    )
    if is_postgres:
        for name, condition in constraints:
            op.create_check_constraint(name, "catalog_sync_runs", condition)
    else:
        with op.batch_alter_table("catalog_sync_runs") as batch_op:
            for name, condition in constraints:
                batch_op.create_check_constraint(name, condition)

    op.create_index(
        "ix_catalog_sync_runs_claimable",
        "catalog_sync_runs",
        ["status", "next_attempt_at", "lease_expires_at", "requested_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"

    op.drop_index("ix_catalog_sync_runs_claimable", table_name="catalog_sync_runs")
    if is_postgres:
        op.drop_constraint("lease_fields_match", "catalog_sync_runs", type_="check")
        op.drop_constraint("attempt_count_nonnegative", "catalog_sync_runs", type_="check")
    else:
        with op.batch_alter_table("catalog_sync_runs") as batch_op:
            batch_op.drop_constraint("lease_fields_match", type_="check")
            batch_op.drop_constraint("attempt_count_nonnegative", type_="check")

    op.drop_column("catalog_sync_runs", "cancel_requested_at")
    op.drop_column("catalog_sync_runs", "next_attempt_at")
    op.drop_column("catalog_sync_runs", "lease_expires_at")
    op.drop_column("catalog_sync_runs", "lease_token")
    op.drop_column("catalog_sync_runs", "lease_owner")
    op.drop_column("catalog_sync_runs", "attempt_count")
