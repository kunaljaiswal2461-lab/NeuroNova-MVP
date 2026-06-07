"""initial: dataset_records

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Check which database engine is currently running
    bind = op.get_bind()
    is_postgres = bind.engine.name == 'postgresql'

# 2. Only run PostgreSQL-specific extensions
    if is_postgres:
        op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        op.execute('CREATE EXTENSION IF NOT EXISTS vector')
        # (Deleted the manual op.execute CREATE TYPE commands here)

    # 3. Use standard SQLAlchemy Enums
    # (Removed create_type=False so SQLAlchemy can create them automatically)
    file_type = sa.Enum("CSV", "XLSX", "JSON", "PARQUET", name="file_type_enum")
    status_enum = sa.Enum("UPLOADED", "QUEUED", "PROFILING", "COMPLETE", "FAILED", name="dataset_status_enum")

    # 4. Handle Postgres-specific UUID defaults
    uuid_default = sa.text("uuid_generate_v4()") if is_postgres else sa.text("(lower(hex(randomblob(16))))")
    
    # 5. Use String(36) for UUIDs in SQLite, native UUID for Postgres
    id_column_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)

    op.create_table(
        "dataset_records",
        sa.Column(
            "id",
            id_column_type,
            primary_key=True,
            server_default=uuid_default,
        ),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("file_type", file_type, nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("col_count", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            status_enum,
            nullable=False,
            server_default="UPLOADED",
        ),
        sa.Column(
            "progress_pct",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_message", sa.String(length=2048), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profiled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_dataset_records_status",
        "dataset_records",
        ["status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == 'postgresql'

    op.drop_index("ix_dataset_records_status", table_name="dataset_records")
    op.drop_table("dataset_records")
    
    if is_postgres:
        op.execute("DROP TYPE IF EXISTS dataset_status_enum")
        op.execute("DROP TYPE IF EXISTS file_type_enum")