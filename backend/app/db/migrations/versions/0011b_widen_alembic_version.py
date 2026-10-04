"""Widen Alembic's revision bookkeeping before long revision identifiers.

Revision ID: 0011b_widen_alembic_version
Revises: 0011_postgres_chat_bindings
Create Date: 2026-10-04

Alembic creates ``alembic_version.version_num`` as ``VARCHAR(32)`` by
default.  The next existing revision identifier is 33 characters, so a fresh
PostgreSQL upgrade otherwise completes its DDL then fails while recording that
revision.  This migration runs while the current value is still short enough
to be stored by the historical column.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011b_widen_alembic_version"
down_revision: Union[str, None] = "0011_postgres_chat_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=255),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        # Alembic records this migration's own short revision value before
        # executing its downgrade, so shrinking is safe on the linear path.
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=255),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
