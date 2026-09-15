"""Persist the direct-query unsupported capability boundary.

Revision ID: 0006_chat_message_unsupported
Revises: 0005_auth_tables
Create Date: 2026-09-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_chat_message_unsupported"
down_revision: Union[str, None] = "0005_auth_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    default = sa.text("false") if bind.engine.name == "postgresql" else sa.text("0")
    op.add_column(
        "chat_messages",
        sa.Column("unsupported", sa.Boolean(), nullable=False, server_default=default),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "unsupported")
