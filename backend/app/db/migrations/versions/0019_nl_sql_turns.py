"""Hybrid database chat: SQL turns, result-row consent and personal-column flags (SPEC-09).

Revision ID: 0019_nl_sql_turns
Revises: 0018_catalog_column_samples
Create Date: 2026-10-12

* ``chat_messages.nl_sql`` (JSONB, null): the SQL, assumptions, repair record and presentation spec of a
  model-written-SQL turn. SQL is stored here and nowhere else (``postgres_query_runs`` keeps none).
* ``data_sources.result_rows_to_model_enabled`` (default false) and ``..._generation``: per-source consent to send
  result rows to the answer-writing model. Owner decision 2: off until the owner turns it on.
* ``source_column_privacy``: the owner's per-column override of the personal-data suggestion, by name.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.base import NAMING_CONVENTION


revision: str = "0019_nl_sql_turns"
down_revision: Union[str, None] = "0018_catalog_column_samples"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GENERATION_CHECK = "ck_data_sources_result_rows_to_model_generation_nonnegative"
SHORT_CHECK = "result_rows_to_model_generation_nonnegative"


def upgrade() -> None:
    is_postgres = op.get_bind().engine.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()
    now = sa.text("now()") if is_postgres else sa.text("(CURRENT_TIMESTAMP)")

    op.add_column("chat_messages", sa.Column("nl_sql", json_type, nullable=True))

    enabled = sa.Column(
        "result_rows_to_model_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
    )
    generation = sa.Column(
        "result_rows_to_model_generation", sa.Integer(), nullable=False, server_default=sa.text("0")
    )
    if is_postgres:
        op.add_column("data_sources", enabled)
        op.add_column("data_sources", generation)
        op.create_check_constraint(SHORT_CHECK, "data_sources", "result_rows_to_model_generation >= 0")
    else:
        with op.batch_alter_table("data_sources", recreate="always", naming_convention=NAMING_CONVENTION) as batch_op:
            batch_op.add_column(enabled)
            batch_op.add_column(generation)
            batch_op.create_check_constraint(SHORT_CHECK, "result_rows_to_model_generation >= 0")

    op.create_table(
        "source_column_privacy",
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("schema_name", sa.String(length=63), nullable=False),
        sa.Column("table_name", sa.String(length=63), nullable=False),
        sa.Column("column_name", sa.String(length=63), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("updated_by_user_id", uuid_type, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint(
            "source_id", "schema_name", "table_name", "column_name", name="pk_source_column_privacy"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["data_sources.id"], name="fk_source_column_privacy_source", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], name="fk_source_column_privacy_user", ondelete="SET NULL"
        ),
        sa.CheckConstraint("state IN ('personal','not_personal')", name="state_valid"),
    )


def downgrade() -> None:
    is_postgres = op.get_bind().engine.name == "postgresql"
    op.drop_table("source_column_privacy")
    if is_postgres:
        op.drop_constraint(op.f(GENERATION_CHECK), "data_sources", type_="check")
        op.drop_column("data_sources", "result_rows_to_model_generation")
        op.drop_column("data_sources", "result_rows_to_model_enabled")
    else:
        with op.batch_alter_table("data_sources", recreate="always", naming_convention=NAMING_CONVENTION) as batch_op:
            batch_op.drop_constraint(op.f(GENERATION_CHECK), type_="check")
            batch_op.drop_column("result_rows_to_model_generation")
            batch_op.drop_column("result_rows_to_model_enabled")
    op.drop_column("chat_messages", "nl_sql")
