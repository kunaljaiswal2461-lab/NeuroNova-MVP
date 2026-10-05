"""Record when a password was proven by the owner of the account's inbox.

Revision ID: 0016_password_provenance
Revises: 0015_stage7_source_operations
Create Date: 2026-10-05

No backfill: the provenance of every existing password is unknown (rows
auto-verified in dev mode look identical to rows verified through the emailed
link), so every existing password row starts unproven (NULL).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016_password_provenance"
down_revision: Union[str, None] = "0015_stage7_source_operations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_proven_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Sessions minted from unproven passwords must not outlive this deploy.
    op.execute(
        """
        UPDATE refresh_tokens
           SET revoked = true
         WHERE revoked = false
           AND user_id IN (SELECT id FROM users WHERE hashed_password IS NOT NULL)
        """
    )


def downgrade() -> None:
    op.drop_column("users", "password_proven_at")
    # Revoked refresh tokens are NOT restored; revocation is the safe state.
