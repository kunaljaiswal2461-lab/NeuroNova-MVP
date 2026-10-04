"""Persist owner-confirmed, immutable same-source relationship definitions.

Revision ID: 0013_confirmed_relationship_definitions
Revises: 0012_confirmed_metric_definitions
Create Date: 2026-10-03

The version payload stores only typed catalog structure for one declared
base-to-lookup foreign-key direction.  It contains no SQL, credentials, bound
values, inferred joins, result rows, or arbitrary expressions.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0013_confirmed_relationship_definitions"
down_revision: Union[str, None] = "0012_confirmed_metric_definitions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LIFECYCLE_ENUM = "relationship_lifecycle_state_enum"
JOIN_ENUM = "relationship_join_type_enum"
RUN_RELATIONSHIP_VERSION_FK = "fk_postgres_query_runs_relationship_definition_version"
RUN_RELATIONSHIP_VERSION_SOURCE_FK = (
    "fk_postgres_query_runs_relationship_definition_version_source"
)
RUN_RELATIONSHIP_VERSION_INDEX = "ix_postgres_query_runs_relationship_definition_version_id"


def _types() -> tuple[sa.Enum, sa.Enum]:
    return (
        sa.Enum("ACTIVE", "NEEDS_REVIEW", "RETIRED", name=LIFECYCLE_ENUM),
        sa.Enum("INNER", "LEFT", name=JOIN_ENUM),
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
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()
    lifecycle, join_type = _types()

    op.create_table(
        "relationship_definitions",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("owner_user_id", uuid_type, nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("identity_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_state", lifecycle, nullable=False, server_default="ACTIVE"),
        sa.Column("current_version_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("needs_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("needs_review_reason", sa.String(length=64), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", uuid_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("current_version_number > 0", name="current_version_positive"),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_relationship_definitions_source_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_user_id"],
            ["users.id"],
            name="fk_relationship_definitions_retired_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("source_id", "id", name="uq_relationship_definitions_source_id_id"),
    )
    op.create_index("ix_relationship_definitions_source_id", "relationship_definitions", ["source_id"])
    op.create_index("ix_relationship_definitions_owner_user_id", "relationship_definitions", ["owner_user_id"])
    op.create_index(
        "uq_relationship_definitions_source_active_identity",
        "relationship_definitions",
        ["source_id", "identity_fingerprint"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'ACTIVE'"),
        sqlite_where=sa.text("lifecycle_state = 'ACTIVE'"),
    )

    op.create_table(
        "relationship_definition_versions",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("relationship_definition_id", uuid_type, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("confirmed_snapshot_id", uuid_type, nullable=False),
        sa.Column("foreign_key_id", uuid_type, nullable=False),
        sa.Column("base_asset_id", uuid_type, nullable=False),
        sa.Column("lookup_asset_id", uuid_type, nullable=False),
        sa.Column("target_key_id", uuid_type, nullable=False),
        sa.Column("join_type", join_type, nullable=False),
        sa.Column("definition_json", json_type, nullable=False),
        sa.Column("confirmed_by_user_id", uuid_type, nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version_number > 0", name="version_positive"),
        sa.CheckConstraint("base_asset_id <> lookup_asset_id", name="base_and_lookup_are_distinct"),
        sa.ForeignKeyConstraint(
            ["source_id", "relationship_definition_id"],
            ["relationship_definitions.source_id", "relationship_definitions.id"],
            name="fk_relationship_definition_versions_definition_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_id",
                "confirmed_snapshot_id",
                "base_asset_id",
                "lookup_asset_id",
                "foreign_key_id",
            ],
            [
                "catalog_foreign_keys.source_id",
                "catalog_foreign_keys.snapshot_id",
                "catalog_foreign_keys.referencing_asset_id",
                "catalog_foreign_keys.referenced_asset_id",
                "catalog_foreign_keys.id",
            ],
            name="fk_relationship_definition_versions_catalog_foreign_key",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "confirmed_snapshot_id", "lookup_asset_id", "target_key_id"],
            [
                "catalog_declared_keys.source_id",
                "catalog_declared_keys.snapshot_id",
                "catalog_declared_keys.asset_id",
                "catalog_declared_keys.id",
            ],
            name="fk_relationship_definition_versions_catalog_target_key",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "confirmed_by_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_relationship_definition_versions_confirming_owner",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "relationship_definition_id",
            "version_number",
            name="uq_relationship_definition_versions_number",
        ),
        sa.UniqueConstraint(
            "source_id", "id", name="uq_relationship_definition_versions_source_id_id"
        ),
    )
    op.create_index(
        "ix_relationship_definition_versions_source_id",
        "relationship_definition_versions",
        ["source_id"],
    )
    op.create_index(
        "ix_relationship_definition_versions_relationship_definition_id",
        "relationship_definition_versions",
        ["relationship_definition_id"],
    )

    # The run remains source-owned even if a relationship version is ever
    # removed.  The simple SET NULL FK clears only the optional version leg;
    # the composite reference blocks cross-source provenance without trying
    # to NULL the required source ID.
    if is_postgres:
        op.add_column(
            "postgres_query_runs",
            sa.Column("relationship_definition_version_id", uuid_type, nullable=True),
        )
        op.create_foreign_key(
            RUN_RELATIONSHIP_VERSION_FK,
            "postgres_query_runs",
            "relationship_definition_versions",
            ["relationship_definition_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            RUN_RELATIONSHIP_VERSION_SOURCE_FK,
            "postgres_query_runs",
            "relationship_definition_versions",
            ["source_id", "relationship_definition_version_id"],
            ["source_id", "id"],
        )
        op.create_index(
            RUN_RELATIONSHIP_VERSION_INDEX,
            "postgres_query_runs",
            ["relationship_definition_version_id"],
        )
    else:
        # SQLite requires a table recreate for new foreign keys.
        with op.batch_alter_table("postgres_query_runs", recreate="always") as batch_op:
            batch_op.add_column(
                sa.Column("relationship_definition_version_id", uuid_type, nullable=True)
            )
            batch_op.create_foreign_key(
                RUN_RELATIONSHIP_VERSION_FK,
                "relationship_definition_versions",
                ["relationship_definition_version_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_foreign_key(
                RUN_RELATIONSHIP_VERSION_SOURCE_FK,
                "relationship_definition_versions",
                ["source_id", "relationship_definition_version_id"],
                ["source_id", "id"],
            )
            batch_op.create_index(
                RUN_RELATIONSHIP_VERSION_INDEX,
                ["relationship_definition_version_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"

    if is_postgres:
        op.drop_index(RUN_RELATIONSHIP_VERSION_INDEX, table_name="postgres_query_runs")
        op.drop_constraint(
            RUN_RELATIONSHIP_VERSION_SOURCE_FK,
            "postgres_query_runs",
            type_="foreignkey",
        )
        op.drop_constraint(
            RUN_RELATIONSHIP_VERSION_FK,
            "postgres_query_runs",
            type_="foreignkey",
        )
        op.drop_column("postgres_query_runs", "relationship_definition_version_id")
    else:
        with op.batch_alter_table("postgres_query_runs", recreate="always") as batch_op:
            batch_op.drop_index(RUN_RELATIONSHIP_VERSION_INDEX)
            batch_op.drop_constraint(RUN_RELATIONSHIP_VERSION_SOURCE_FK, type_="foreignkey")
            batch_op.drop_constraint(RUN_RELATIONSHIP_VERSION_FK, type_="foreignkey")
            batch_op.drop_column("relationship_definition_version_id")

    op.drop_index(
        "ix_relationship_definition_versions_relationship_definition_id",
        table_name="relationship_definition_versions",
    )
    op.drop_index(
        "ix_relationship_definition_versions_source_id",
        table_name="relationship_definition_versions",
    )
    op.drop_table("relationship_definition_versions")
    op.drop_index(
        "uq_relationship_definitions_source_active_identity",
        table_name="relationship_definitions",
    )
    op.drop_index(
        "ix_relationship_definitions_owner_user_id", table_name="relationship_definitions"
    )
    op.drop_index("ix_relationship_definitions_source_id", table_name="relationship_definitions")
    op.drop_table("relationship_definitions")
    if is_postgres:
        postgresql.ENUM(name=JOIN_ENUM).drop(bind, checkfirst=True)
        postgresql.ENUM(name=LIFECYCLE_ENUM).drop(bind, checkfirst=True)
