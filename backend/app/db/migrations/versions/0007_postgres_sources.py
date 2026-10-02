"""Persist creator-owned PostgreSQL onboarding sources.

Revision ID: 0007_postgres_sources
Revises: 0006_chat_message_unsupported
Create Date: 2026-10-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0007_postgres_sources"
down_revision: Union[str, None] = "0006_chat_message_unsupported"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SOURCE_TYPE_ENUM = "source_type_enum"
SOURCE_STATUS_ENUM = "source_status_enum"
SOURCE_TLS_MODE_ENUM = "source_tls_mode_enum"


def _types() -> tuple[sa.Enum, sa.Enum, sa.Enum]:
    return (
        sa.Enum("POSTGRESQL", name=SOURCE_TYPE_ENUM),
        sa.Enum(
            "TESTED",
            "CONNECTION_FAILED",
            "CREDENTIAL_UNAVAILABLE",
            name=SOURCE_STATUS_ENUM,
        ),
        sa.Enum("VERIFY_FULL", name=SOURCE_TLS_MODE_ENUM),
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
    bool_default = sa.text("false") if is_postgres else sa.text("0")
    source_type, source_status, tls_mode = _types()

    op.create_table(
        "data_sources",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column(
            "owner_user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_type", source_type, nullable=False, server_default="POSTGRESQL"),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default=sa.text("5432")),
        sa.Column("database_name", sa.String(length=63), nullable=False),
        sa.Column("tls_mode", tls_mode, nullable=False, server_default="VERIFY_FULL"),
        sa.Column("has_custom_ca", sa.Boolean(), nullable=False, server_default=bool_default),
        sa.Column("status", source_status, nullable=False, server_default="TESTED"),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profiling_enabled", sa.Boolean(), nullable=False, server_default=bool_default),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_data_sources_owner_user_id", "data_sources", ["owner_user_id"])

    op.create_table(
        "source_credentials",
        sa.Column(
            "source_id",
            uuid_type,
            sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("encryption_format", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "source_table_scopes",
        sa.Column(
            "source_id",
            uuid_type,
            sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("schema_name", sa.String(length=63), primary_key=True),
        sa.Column("table_name", sa.String(length=63), primary_key=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"
    op.drop_table("source_table_scopes")
    op.drop_table("source_credentials")
    op.drop_index("ix_data_sources_owner_user_id", table_name="data_sources")
    op.drop_table("data_sources")

    if is_postgres:
        source_type, source_status, tls_mode = _types()
        tls_mode.drop(bind, checkfirst=True)
        source_status.drop(bind, checkfirst=True)
        source_type.drop(bind, checkfirst=True)
