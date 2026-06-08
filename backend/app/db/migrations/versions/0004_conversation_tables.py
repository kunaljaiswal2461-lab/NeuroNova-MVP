"""Layer 7 — conversation_sessions + chat_messages tables.

Revision ID: 0004_conversation_tables
Revises: 0003_finding_embeddings
Create Date: 2026-05-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0004_conversation_tables"
down_revision: Union[str, None] = "0003_finding_embeddings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONVERSATION_MODE_ENUM = "conversation_mode_enum"
_CHAT_ROLE_ENUM = "chat_role_enum"


def upgrade() -> None:
    # ─── 0. Check Database Dialect ───────────────────────────────────────
    bind = op.get_bind()
    is_postgres = bind.engine.name == 'postgresql'

    # ─── 1. Fallbacks and Types ──────────────────────────────────────────
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    uuid_default = sa.text("uuid_generate_v4()") if is_postgres else sa.text("(lower(hex(randomblob(16))))")
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()
    
    # Let SQLAlchemy natively handle database-level enum registration
    mode_enum = sa.Enum("AUTO", "RAG", "QUERY", name=_CONVERSATION_MODE_ENUM)
    role_enum = sa.Enum("USER", "ASSISTANT", name=_CHAT_ROLE_ENUM)
    
    mode_default = sa.text("'AUTO'::conversation_mode_enum") if is_postgres else "AUTO"
    boolean_default = sa.text("false") if is_postgres else "0"

    # ─── 2. conversation_sessions ────────────────────────────────────────
    op.create_table(
        "conversation_sessions",
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
            "mode",
            mode_enum,
            nullable=False,
            server_default=mode_default,
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "message_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_conversation_sessions_dataset_id",
        "conversation_sessions",
        ["dataset_id"],
    )

    # ─── 3. chat_messages ────────────────────────────────────────────────
    op.create_table(
        "chat_messages",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=uuid_default,
        ),
        sa.Column(
            "session_id",
            uuid_type,
            sa.ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            role_enum,
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("mode_used", sa.String(length=16), nullable=True),
        sa.Column("intent", json_type, nullable=True),
        sa.Column("citations", json_type, nullable=True),
        sa.Column("query_result", json_type, nullable=True),
        sa.Column(
            "degraded",
            sa.Boolean(),
            nullable=False,
            server_default=boolean_default,
        ),
        sa.Column("degraded_reason", sa.Text(), nullable=True),
        sa.Column("token_input", sa.Integer(), nullable=True),
        sa.Column("token_output", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_chat_messages_session_id",
        "chat_messages",
        ["session_id"],
    )
    op.create_index(
        "ix_chat_messages_session_id_created_at",
        "chat_messages",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == 'postgresql'

    op.drop_index("ix_chat_messages_session_id_created_at", table_name="chat_messages")
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_conversation_sessions_dataset_id", table_name="conversation_sessions")
    op.drop_table("conversation_sessions")
    
    if is_postgres:
        op.execute(f"DROP TYPE IF EXISTS {_CHAT_ROLE_ENUM}")
        op.execute(f"DROP TYPE IF EXISTS {_CONVERSATION_MODE_ENUM}")