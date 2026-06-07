"""Add FINDINGS, VIZ, INSIGHTS to dataset_status_enum

Revision ID: 0002_pipeline_status_values
Revises: 0001_initial
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_pipeline_status_values"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Check if we are running Postgres
    bind = op.get_bind()
    if bind.engine.name == 'postgresql':
        # Only run these on Postgres. SQLite treats these columns as plain text, 
        # so it doesn't need to explicitly "alter" the accepted values.
        op.execute("ALTER TYPE dataset_status_enum ADD VALUE IF NOT EXISTS 'FINDINGS'")
        op.execute("ALTER TYPE dataset_status_enum ADD VALUE IF NOT EXISTS 'VIZ'")
        op.execute("ALTER TYPE dataset_status_enum ADD VALUE IF NOT EXISTS 'INSIGHTS'")


def downgrade() -> None:
    # Downgrading ENUMs in Postgres is notoriously complex, 
    # so we typically leave them as is or write a complex raw SQL script.
    pass