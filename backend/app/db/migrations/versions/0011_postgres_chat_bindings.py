"""Bind conversations to exactly one dataset or creator-owned source.

Revision ID: 0011_postgres_chat_bindings
Revises: 0010_postgres_query_execution
Create Date: 2026-10-03

Existing uploaded-dataset conversations are preserved unchanged.  New source
bindings are constrained both by an XOR resource check and by the source
owner's composite key.  Deleting a source therefore cascades its sessions and
messages instead of leaving historical results accessible after source access
is revoked.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0011_postgres_chat_bindings"
down_revision: Union[str, None] = "0010_postgres_query_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BINDING_CHECK = (
    "(dataset_id IS NOT NULL AND source_id IS NULL) "
    "OR (dataset_id IS NULL AND source_id IS NOT NULL AND user_id IS NOT NULL)"
)
_SOURCE_FK = "fk_conversation_sessions_source_id_data_sources"
_SOURCE_OWNER_FK = "fk_conversation_sessions_source_owner"
_BINDING_CHECK_NAME = "ck_conversation_sessions_exactly_one_binding"
_BINDING_CHECK_TOKEN = "exactly_one_binding"
_SOURCE_INDEX = "ix_conversation_sessions_source_id"


def _source_session_count() -> int:
    """Count data that the pre-Stage-4C schema cannot represent on downgrade."""
    bind = op.get_bind()
    return int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM conversation_sessions "
                "WHERE source_id IS NOT NULL"
            )
        ).scalar()
        or 0
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()

    if is_postgres:
        # Existing rows are dataset-bound, so making the column nullable is a
        # metadata-only safe transition before introducing source bindings.
        op.alter_column(
            "conversation_sessions",
            "dataset_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
        op.add_column(
            "conversation_sessions",
            sa.Column("source_id", uuid_type, nullable=True),
        )
        op.create_foreign_key(
            op.f(_SOURCE_FK),
            "conversation_sessions",
            "data_sources",
            ["source_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            op.f(_SOURCE_OWNER_FK),
            "conversation_sessions",
            "data_sources",
            ["source_id", "user_id"],
            ["id", "owner_user_id"],
            ondelete="CASCADE",
        )
        op.create_check_constraint(
            _BINDING_CHECK_TOKEN, "conversation_sessions", _BINDING_CHECK
        )
        op.create_index(_SOURCE_INDEX, "conversation_sessions", ["source_id"])
    else:
        # SQLite cannot alter nullability or add a table check/foreign key in
        # place.  Alembic's recreate strategy copies every existing
        # dataset-bound row into the equivalent schema in one transaction.
        with op.batch_alter_table("conversation_sessions", recreate="always") as batch_op:
            batch_op.alter_column(
                "dataset_id", existing_type=sa.String(length=36), nullable=True
            )
            batch_op.add_column(sa.Column("source_id", uuid_type, nullable=True))
            batch_op.create_foreign_key(
                _SOURCE_FK,
                "data_sources",
                ["source_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch_op.create_foreign_key(
                _SOURCE_OWNER_FK,
                "data_sources",
                ["source_id", "user_id"],
                ["id", "owner_user_id"],
                ondelete="CASCADE",
            )
            batch_op.create_check_constraint(_BINDING_CHECK_TOKEN, _BINDING_CHECK)
            batch_op.create_index(_SOURCE_INDEX, ["source_id"])

    op.add_column("chat_messages", sa.Column("postgres_result", json_type, nullable=True))


def downgrade() -> None:
    # A source-bound session has no valid representation in the old schema.
    # Refuse rather than silently delete the session and its transcript.
    if _source_session_count():
        raise RuntimeError(
            "cannot downgrade 0011_postgres_chat_bindings while source-bound "
            "conversation sessions exist"
        )

    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"

    if is_postgres:
        op.drop_column("chat_messages", "postgres_result")
        op.drop_index(_SOURCE_INDEX, table_name="conversation_sessions")
        op.drop_constraint(op.f(_SOURCE_OWNER_FK), "conversation_sessions", type_="foreignkey")
        op.drop_constraint(op.f(_SOURCE_FK), "conversation_sessions", type_="foreignkey")
        op.drop_constraint(op.f(_BINDING_CHECK_NAME), "conversation_sessions", type_="check")
        op.drop_column("conversation_sessions", "source_id")
        op.alter_column(
            "conversation_sessions",
            "dataset_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )
    else:
        with op.batch_alter_table("chat_messages", recreate="always") as batch_op:
            batch_op.drop_column("postgres_result")
        with op.batch_alter_table("conversation_sessions", recreate="always") as batch_op:
            batch_op.drop_index(_SOURCE_INDEX)
            batch_op.drop_constraint(op.f(_SOURCE_OWNER_FK), type_="foreignkey")
            batch_op.drop_constraint(op.f(_SOURCE_FK), type_="foreignkey")
            batch_op.drop_constraint(op.f(_BINDING_CHECK_NAME), type_="check")
            batch_op.drop_column("source_id")
            batch_op.alter_column(
                "dataset_id", existing_type=sa.String(length=36), nullable=False
            )
