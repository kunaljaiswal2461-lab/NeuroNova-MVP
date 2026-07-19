"""Auth system — users, refresh_tokens, user_id FKs on datasets and sessions.

Revision ID: 0005_auth_tables
Revises: 0004_conversation_tables
Create Date: 2026-06-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0005_auth_tables"
down_revision: Union[str, None] = "0004_conversation_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_USER_ROLE_ENUM = "user_role_enum"


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"

    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    uuid_default = sa.text("uuid_generate_v4()") if is_postgres else sa.text("(lower(hex(randomblob(16))))")

    role_enum = sa.Enum("USER", "ADMIN", name=_USER_ROLE_ENUM)

    # ── 1. users ──────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("hashed_password", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("role", role_enum, nullable=False, server_default="USER"),
        sa.Column("google_id", sa.String(256), nullable=True),
        sa.Column("avatar_url", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_google_id", "users", ["google_id"], unique=True)

    # ── 2. refresh_tokens ─────────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)

    # ── 3. user_id FK on dataset_records (nullable for existing rows) ─────────
    op.add_column(
        "dataset_records",
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_dataset_records_user_id", "dataset_records", ["user_id"])

    # ── 4. user_id FK on conversation_sessions (nullable for existing rows) ───
    op.add_column(
        "conversation_sessions",
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_conversation_sessions_user_id", "conversation_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_sessions_user_id", table_name="conversation_sessions")
    op.drop_column("conversation_sessions", "user_id")

    op.drop_index("ix_dataset_records_user_id", table_name="dataset_records")
    op.drop_column("dataset_records", "user_id")

    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index("ix_users_google_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        op.execute(f"DROP TYPE IF EXISTS {_USER_ROLE_ENUM}")
