"""Store example values of text columns per catalog snapshot (SPEC-07).

Revision ID: 0018_catalog_column_samples
Revises: 0017_sql_query_runs
Create Date: 2026-10-09

Owner decisions D1-D3 (2026-10-09): values are stored in the application database, collected on each catalog
sync and tied to that snapshot's columns; collection is on by default and the owner can switch it off per
source (which deletes stored values); personal data stays out by auto-detection plus owner column exclusions.

This reverses the Stage 6A statement that text values are never persisted, for this feature only.
``catalog_columns`` gets a unique key on (source_id, snapshot_id, id): PostgreSQL requires the exact column set
that a composite foreign key references to be unique, and the existing key also contains ``asset_id``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0018_catalog_column_samples"
down_revision: Union[str, None] = "0017_sql_query_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GENERATION_CHECK = "ck_data_sources_sample_values_generation_nonnegative"
COLUMN_UNIQUE = "uq_catalog_columns_source_snapshot_id"


def upgrade() -> None:
    is_postgres = op.get_bind().engine.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()
    now = sa.text("now()") if is_postgres else sa.text("(CURRENT_TIMESTAMP)")
    enabled = sa.Column(
        "sample_values_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
    )
    generation = sa.Column(
        "sample_values_generation", sa.Integer(), nullable=False, server_default=sa.text("0")
    )

    if is_postgres:
        op.add_column("data_sources", enabled)
        op.add_column("data_sources", generation)
        op.create_check_constraint(
            "sample_values_generation_nonnegative", "data_sources", "sample_values_generation >= 0"
        )
        op.create_unique_constraint(
            COLUMN_UNIQUE, "catalog_columns", ["source_id", "snapshot_id", "id"]
        )
    else:
        with op.batch_alter_table("data_sources", recreate="always") as batch_op:
            batch_op.add_column(enabled)
            batch_op.add_column(generation)
            batch_op.create_check_constraint(
                "sample_values_generation_nonnegative", "sample_values_generation >= 0"
            )
        with op.batch_alter_table("catalog_columns", recreate="always") as batch_op:
            batch_op.create_unique_constraint(COLUMN_UNIQUE, ["source_id", "snapshot_id", "id"])

    op.add_column("catalog_snapshots", sa.Column("sample_skips", json_type, nullable=True))

    op.create_table(
        "catalog_column_samples",
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("column_id", uuid_type, nullable=False),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("values", json_type, nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("coverage", sa.Numeric(5, 4), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_values_generation", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id", "column_id", name="pk_catalog_column_samples"),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "column_id"],
            ["catalog_columns.source_id", "catalog_columns.snapshot_id", "catalog_columns.id"],
            name="fk_catalog_column_samples_column",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("method IN ('pg_stats','exact')", name="method_valid"),
    )
    op.create_index("ix_catalog_column_samples_source", "catalog_column_samples", ["source_id"])

    op.create_table(
        "source_sample_exclusions",
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("schema_name", sa.String(length=63), nullable=False),
        sa.Column("table_name", sa.String(length=63), nullable=False),
        sa.Column("column_name", sa.String(length=63), nullable=False),
        sa.Column("created_by_user_id", uuid_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint(
            "source_id", "schema_name", "table_name", "column_name", name="pk_source_sample_exclusions"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["data_sources.id"], name="fk_source_sample_exclusions_source", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name="fk_source_sample_exclusions_user", ondelete="SET NULL"
        ),
    )


def downgrade() -> None:
    """Drops the two tables and the three columns. No data in them is sacred."""
    is_postgres = op.get_bind().engine.name == "postgresql"
    op.drop_table("source_sample_exclusions")
    op.drop_index("ix_catalog_column_samples_source", table_name="catalog_column_samples")
    op.drop_table("catalog_column_samples")
    if is_postgres:
        op.drop_column("catalog_snapshots", "sample_skips")
        op.drop_constraint(COLUMN_UNIQUE, "catalog_columns", type_="unique")
        op.drop_constraint(op.f(GENERATION_CHECK), "data_sources", type_="check")
        op.drop_column("data_sources", "sample_values_generation")
        op.drop_column("data_sources", "sample_values_enabled")
    else:
        with op.batch_alter_table("catalog_snapshots", recreate="always") as batch_op:
            batch_op.drop_column("sample_skips")
        with op.batch_alter_table("catalog_columns", recreate="always") as batch_op:
            batch_op.drop_constraint(COLUMN_UNIQUE, type_="unique")
        with op.batch_alter_table("data_sources", recreate="always") as batch_op:
            batch_op.drop_constraint(op.f(GENERATION_CHECK), type_="check")
            batch_op.drop_column("sample_values_generation")
            batch_op.drop_column("sample_values_enabled")
