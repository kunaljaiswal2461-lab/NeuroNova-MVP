"""Let postgres_query_runs record validated-SQL runs (SPEC-05).

Revision ID: 0017_sql_query_runs
Revises: 0016_password_provenance
Create Date: 2026-10-08

A raw SELECT can read several tables, so a 'sql' run has no single asset: ``asset_id`` becomes nullable
(guarded by a check tied to ``query_kind``) and the assets it read go in ``postgres_query_run_assets``.
No SQL text and no literal value is stored anywhere; ``plan_fingerprint`` holds the structural
fingerprint (literals replaced by placeholders) for sql runs.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0017_sql_query_runs"
down_revision: Union[str, None] = "0016_password_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KIND_CHECK = "query_kind_valid"
KIND_ASSET_CHECK = "query_kind_asset_consistent"
KIND_EXPR = "query_kind IN ('compiled','sql')"
KIND_ASSET_EXPR = (
    "(query_kind = 'compiled' AND asset_id IS NOT NULL) OR (query_kind = 'sql' AND asset_id IS NULL)"
)


def _uuid_type(is_postgres: bool):
    return postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)


def upgrade() -> None:
    is_postgres = op.get_bind().engine.name == "postgresql"
    uuid_type = _uuid_type(is_postgres)
    kind = sa.Column("query_kind", sa.String(length=16), nullable=False, server_default="compiled")
    if is_postgres:
        op.add_column("postgres_query_runs", kind)
        op.alter_column("postgres_query_runs", "asset_id", existing_type=uuid_type, nullable=True)
        op.create_check_constraint(KIND_CHECK, "postgres_query_runs", KIND_EXPR)
        op.create_check_constraint(KIND_ASSET_CHECK, "postgres_query_runs", KIND_ASSET_EXPR)
        op.execute(
            "COMMENT ON COLUMN postgres_query_runs.plan_fingerprint IS "
            "'Structural fingerprint: Stage 4A plan fingerprint (compiled) or SQL with literals replaced "
            "by placeholders (sql). Never a SQL-text or parameter fingerprint.'"
        )
    else:
        with op.batch_alter_table("postgres_query_runs", recreate="always") as batch_op:
            batch_op.add_column(kind)
            batch_op.alter_column("asset_id", existing_type=uuid_type, nullable=True)
            batch_op.create_check_constraint(KIND_CHECK, KIND_EXPR)
            batch_op.create_check_constraint(KIND_ASSET_CHECK, KIND_ASSET_EXPR)

    op.create_table(
        "postgres_query_run_assets",
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.PrimaryKeyConstraint("run_id", "asset_id", name="pk_postgres_query_run_assets"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["postgres_query_runs.id"],
            name="fk_postgres_query_run_assets_run_id_postgres_query_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_postgres_query_run_assets_source_snapshot_asset",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"
    uuid_type = _uuid_type(is_postgres)
    # A sql run has no asset_id; restoring NOT NULL would have to invent one. Refuse instead.
    remaining = bind.execute(
        sa.text("SELECT count(*) FROM postgres_query_runs WHERE query_kind = 'sql'")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            "Refusing to downgrade: postgres_query_runs holds query_kind='sql' rows. "
            "Delete them first if their loss is intended."
        )
    op.drop_table("postgres_query_run_assets")
    if is_postgres:
        op.execute("COMMENT ON COLUMN postgres_query_runs.plan_fingerprint IS NULL")
        op.drop_constraint(KIND_ASSET_CHECK, "postgres_query_runs", type_="check")
        op.drop_constraint(KIND_CHECK, "postgres_query_runs", type_="check")
        op.alter_column("postgres_query_runs", "asset_id", existing_type=uuid_type, nullable=False)
        op.drop_column("postgres_query_runs", "query_kind")
    else:
        with op.batch_alter_table("postgres_query_runs", recreate="always") as batch_op:
            batch_op.drop_constraint(KIND_ASSET_CHECK, type_="check")
            batch_op.drop_constraint(KIND_CHECK, type_="check")
            batch_op.alter_column("asset_id", existing_type=uuid_type, nullable=False)
            batch_op.drop_column("query_kind")
