"""Layer 6 — finding_embeddings table + INDEXING status.

Revision ID: 0003_finding_embeddings
Revises: 0002_pipeline_status_values
Create Date: 2026-05-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0003_finding_embeddings"
down_revision: Union[str, None] = "0002_pipeline_status_values"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_IVFFLAT_LISTS = 100
_EMBEDDING_DIM = 1536


def upgrade() -> None:
    # ─── 0. Check Database Dialect ───────────────────────────────────────
    bind = op.get_bind()
    is_postgres = bind.engine.name == 'postgresql'

    # ─── 1. Extend the status enum (must run outside a transaction) ──────
    if is_postgres:
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE dataset_status_enum ADD VALUE IF NOT EXISTS 'INDEXING'"
            )

    # ─── 2. Create the embeddings table ──────────────────────────────────
    # Fallbacks for SQLite: String instead of UUID, JSON instead of ARRAY
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    uuid_default = sa.text("uuid_generate_v4()") if is_postgres else sa.text("(lower(hex(randomblob(16))))")
    array_type = sa.dialects.postgresql.ARRAY(sa.Float()) if is_postgres else sa.JSON()

    op.create_table(
        "finding_embeddings",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=uuid_default,
        ),
        sa.Column(
            "dataset_id",
            uuid_type,
            sa.ForeignKey("dataset_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "finding_id",
            uuid_type,
            nullable=False,
        ),
        sa.Column(
            "embedding",
            array_type,
            nullable=False,
        ),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("embedded_text", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("finding_type", sa.String(length=64), nullable=False),
        sa.Column("column_name", sa.String(length=256), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "dataset_id", "finding_id",
            name="uq_finding_embeddings_dataset_id_finding_id",
        ),
    )

    if is_postgres:
        # Rewrite to real vector(N) only in Postgres
        op.execute(
            f"ALTER TABLE finding_embeddings "
            f"ALTER COLUMN embedding TYPE vector({_EMBEDDING_DIM}) "
            f"USING embedding::vector({_EMBEDDING_DIM})"
        )

    # ─── 3. Pre-filter indexes (btree) ──────────────────────────────────
    op.create_index(
        "ix_finding_embeddings_dataset_id",
        "finding_embeddings",
        ["dataset_id"],
    )
    op.create_index(
        "ix_finding_embeddings_severity",
        "finding_embeddings",
        ["severity"],
    )
    op.create_index(
        "ix_finding_embeddings_finding_type",
        "finding_embeddings",
        ["finding_type"],
    )
    op.create_index(
        "ix_finding_embeddings_column_name",
        "finding_embeddings",
        ["column_name"],
    )

    # ─── 4. Cosine-similarity index (IVFFlat) ────────────────────────────
    if is_postgres:
        op.execute(
            "CREATE INDEX ix_finding_embeddings_embedding "
            "ON finding_embeddings "
            "USING ivfflat (embedding vector_cosine_ops) "
            f"WITH (lists = {_IVFFLAT_LISTS})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == 'postgresql'

    if is_postgres:
        op.execute("DROP INDEX IF EXISTS ix_finding_embeddings_embedding")
        
    op.drop_index("ix_finding_embeddings_column_name", table_name="finding_embeddings")
    op.drop_index("ix_finding_embeddings_finding_type", table_name="finding_embeddings")
    op.drop_index("ix_finding_embeddings_severity", table_name="finding_embeddings")
    op.drop_index("ix_finding_embeddings_dataset_id", table_name="finding_embeddings")
    op.drop_table("finding_embeddings")