"""Persist immutable PostgreSQL source catalog snapshots.

Revision ID: 0008_catalog_persistence
Revises: 0007_postgres_sources
Create Date: 2026-10-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0008_catalog_persistence"
down_revision: Union[str, None] = "0007_postgres_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SYNC_STATUS_ENUM = "catalog_sync_run_status_enum"
KEY_KIND_ENUM = "catalog_key_kind_enum"
FOREIGN_KEY_TARGET_STATUS_ENUM = "catalog_foreign_key_target_status_enum"


def _types() -> tuple[sa.Enum, sa.Enum, sa.Enum]:
    return (
        sa.Enum("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name=SYNC_STATUS_ENUM),
        sa.Enum("PRIMARY_KEY", "UNIQUE", name=KEY_KIND_ENUM),
        sa.Enum("IN_SCOPE", "TARGET_OUT_OF_SCOPE", name=FOREIGN_KEY_TARGET_STATUS_ENUM),
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
    sync_status, key_kind, foreign_key_target_status = _types()

    # This counter is allocated while the source row is locked. It gives
    # publication a deterministic order without a process-local mutex.
    op.add_column(
        "data_sources",
        sa.Column(
            "catalog_sync_sequence",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    if is_postgres:
        op.create_check_constraint(
            "catalog_sync_sequence_nonnegative",
            "data_sources",
            "catalog_sync_sequence >= 0",
        )
    else:
        with op.batch_alter_table("data_sources") as batch_op:
            batch_op.create_check_constraint(
                "catalog_sync_sequence_nonnegative",
                "catalog_sync_sequence >= 0",
            )
    # This unique index is the target of the source/requester ownership FK.
    op.create_index(
        "uq_data_sources_id_owner_user_id",
        "data_sources",
        ["id", "owner_user_id"],
        unique=True,
    )

    op.create_table(
        "catalog_sync_runs",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("requesting_user_id", uuid_type, nullable=False),
        sa.Column("request_sequence", sa.Integer(), nullable=False),
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("scope_count", sa.Integer(), nullable=False),
        sa.Column("status", sync_status, nullable=False, server_default="QUEUED"),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("request_sequence > 0", name="sequence_positive"),
        sa.CheckConstraint("scope_count > 0", name="scope_count_positive"),
        sa.ForeignKeyConstraint(
            ["source_id", "requesting_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_catalog_sync_runs_source_owner",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "request_sequence", name="uq_catalog_sync_runs_source_sequence"),
        sa.UniqueConstraint(
            "source_id",
            "id",
            "scope_fingerprint",
            name="uq_catalog_sync_runs_source_id_id_scope_fingerprint",
        ),
    )
    op.create_index("ix_catalog_sync_runs_source_id", "catalog_sync_runs", ["source_id"])

    op.create_table(
        "catalog_snapshots",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("sync_run_id", uuid_type, nullable=False),
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id", "sync_run_id", "scope_fingerprint"],
            [
                "catalog_sync_runs.source_id",
                "catalog_sync_runs.id",
                "catalog_sync_runs.scope_fingerprint",
            ],
            name="fk_catalog_snapshots_source_run_scope",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "id", name="uq_catalog_snapshots_source_id_id"),
        sa.UniqueConstraint("sync_run_id", name="uq_catalog_snapshots_sync_run_id"),
    )
    op.create_index("ix_catalog_snapshots_source_id", "catalog_snapshots", ["source_id"])
    op.create_index("ix_catalog_snapshots_sync_run_id", "catalog_snapshots", ["sync_run_id"])

    op.create_table(
        "source_catalog_heads",
        sa.Column(
            "source_id",
            uuid_type,
            sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_source_catalog_heads_source_snapshot",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "catalog_assets",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("schema_name", sa.String(length=63), nullable=False),
        sa.Column("table_name", sa.String(length=63), nullable=False),
        sa.Column("table_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_catalog_assets_source_snapshot",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("source_id", "snapshot_id", "id", name="uq_catalog_assets_source_snapshot_id"),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "schema_name",
            "table_name",
            name="uq_catalog_assets_snapshot_table_identity",
        ),
    )

    op.create_table(
        "catalog_columns",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("column_name", sa.String(length=63), nullable=False),
        sa.Column("ordinal_position", sa.Integer(), nullable=False),
        sa.Column("is_nullable", sa.Boolean(), nullable=False, server_default=bool_default),
        sa.Column("formatted_native_type", sa.Text(), nullable=False),
        sa.Column("native_type_schema", sa.String(length=63), nullable=True),
        sa.Column("native_type_name", sa.String(length=63), nullable=True),
        sa.Column("native_type_oid", sa.Integer(), nullable=True),
        sa.Column("native_type_modifier", sa.Integer(), nullable=True),
        sa.Column("column_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("ordinal_position > 0", name="ordinal_positive"),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_columns_source_snapshot_asset",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "id",
            name="uq_catalog_columns_source_snapshot_asset_id",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "column_name",
            name="uq_catalog_columns_asset_name",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "ordinal_position",
            name="uq_catalog_columns_asset_ordinal",
        ),
    )

    op.create_table(
        "catalog_declared_keys",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("constraint_name", sa.String(length=63), nullable=False),
        sa.Column("key_kind", key_kind, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_declared_keys_source_snapshot_asset",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "id",
            name="uq_catalog_declared_keys_source_snapshot_asset_id",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "constraint_name",
            name="uq_catalog_declared_keys_asset_constraint",
        ),
    )
    op.create_index(
        "uq_catalog_declared_keys_primary_per_asset",
        "catalog_declared_keys",
        ["source_id", "snapshot_id", "asset_id"],
        unique=True,
        postgresql_where=sa.text("key_kind = 'PRIMARY_KEY'"),
        sqlite_where=sa.text("key_kind = 'PRIMARY_KEY'"),
    )

    op.create_table(
        "catalog_declared_key_columns",
        sa.Column("source_id", uuid_type, primary_key=True),
        sa.Column("snapshot_id", uuid_type, primary_key=True),
        sa.Column("key_id", uuid_type, primary_key=True),
        sa.Column("ordinal_position", sa.Integer(), primary_key=True),
        sa.Column("asset_id", uuid_type, nullable=False),
        sa.Column("column_id", uuid_type, nullable=False),
        sa.CheckConstraint(
            "ordinal_position > 0", name="ordinal_positive"
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id", "key_id"],
            [
                "catalog_declared_keys.source_id",
                "catalog_declared_keys.snapshot_id",
                "catalog_declared_keys.asset_id",
                "catalog_declared_keys.id",
            ],
            name="fk_catalog_declared_key_columns_key",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id", "column_id"],
            [
                "catalog_columns.source_id",
                "catalog_columns.snapshot_id",
                "catalog_columns.asset_id",
                "catalog_columns.id",
            ],
            name="fk_catalog_declared_key_columns_column",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "key_id",
            "ordinal_position",
            name="uq_catalog_declared_key_columns_key_ordinal",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "key_id",
            "column_id",
            name="uq_catalog_declared_key_columns_key_column",
        ),
    )

    op.create_table(
        "catalog_foreign_keys",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("snapshot_id", uuid_type, nullable=False),
        sa.Column("referencing_asset_id", uuid_type, nullable=False),
        sa.Column("constraint_name", sa.String(length=63), nullable=False),
        sa.Column("target_status", foreign_key_target_status, nullable=False),
        sa.Column("referenced_asset_id", uuid_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(target_status = 'IN_SCOPE' AND referenced_asset_id IS NOT NULL) "
            "OR (target_status = 'TARGET_OUT_OF_SCOPE' AND referenced_asset_id IS NULL)",
            name="target_status_matches_asset",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "referencing_asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_foreign_keys_referencing_asset",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "referenced_asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_foreign_keys_referenced_asset",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "referencing_asset_id",
            "id",
            name="uq_catalog_foreign_keys_source_snapshot_referencing_id",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "referencing_asset_id",
            "referenced_asset_id",
            "id",
            name="uq_catalog_foreign_keys_source_snapshot_full_id",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "referencing_asset_id",
            "constraint_name",
            name="uq_catalog_foreign_keys_asset_constraint",
        ),
    )

    op.create_table(
        "catalog_foreign_key_columns",
        sa.Column("source_id", uuid_type, primary_key=True),
        sa.Column("snapshot_id", uuid_type, primary_key=True),
        sa.Column("foreign_key_id", uuid_type, primary_key=True),
        sa.Column("ordinal_position", sa.Integer(), primary_key=True),
        sa.Column("referencing_asset_id", uuid_type, nullable=False),
        sa.Column("referenced_asset_id", uuid_type, nullable=False),
        sa.Column("referencing_column_id", uuid_type, nullable=False),
        sa.Column("referenced_column_id", uuid_type, nullable=False),
        sa.CheckConstraint(
            "ordinal_position > 0", name="ordinal_positive"
        ),
        sa.ForeignKeyConstraint(
            [
                "source_id",
                "snapshot_id",
                "referencing_asset_id",
                "referenced_asset_id",
                "foreign_key_id",
            ],
            [
                "catalog_foreign_keys.source_id",
                "catalog_foreign_keys.snapshot_id",
                "catalog_foreign_keys.referencing_asset_id",
                "catalog_foreign_keys.referenced_asset_id",
                "catalog_foreign_keys.id",
            ],
            name="fk_catalog_foreign_key_columns_relationship",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "referencing_asset_id", "referencing_column_id"],
            [
                "catalog_columns.source_id",
                "catalog_columns.snapshot_id",
                "catalog_columns.asset_id",
                "catalog_columns.id",
            ],
            name="fk_catalog_foreign_key_columns_referencing_column",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "snapshot_id", "referenced_asset_id", "referenced_column_id"],
            [
                "catalog_columns.source_id",
                "catalog_columns.snapshot_id",
                "catalog_columns.asset_id",
                "catalog_columns.id",
            ],
            name="fk_catalog_foreign_key_columns_referenced_column",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "foreign_key_id",
            "ordinal_position",
            name="uq_catalog_foreign_key_columns_fk_ordinal",
        ),
        sa.UniqueConstraint(
            "source_id",
            "snapshot_id",
            "foreign_key_id",
            "referencing_column_id",
            name="uq_catalog_foreign_key_columns_fk_referencing_column",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.engine.name == "postgresql"

    op.drop_table("catalog_foreign_key_columns")
    op.drop_table("catalog_foreign_keys")
    op.drop_table("catalog_declared_key_columns")
    op.drop_index("uq_catalog_declared_keys_primary_per_asset", table_name="catalog_declared_keys")
    op.drop_table("catalog_declared_keys")
    op.drop_table("catalog_columns")
    op.drop_table("catalog_assets")
    op.drop_table("source_catalog_heads")
    op.drop_index("ix_catalog_snapshots_sync_run_id", table_name="catalog_snapshots")
    op.drop_index("ix_catalog_snapshots_source_id", table_name="catalog_snapshots")
    op.drop_table("catalog_snapshots")
    op.drop_index("ix_catalog_sync_runs_source_id", table_name="catalog_sync_runs")
    op.drop_table("catalog_sync_runs")

    op.drop_index("uq_data_sources_id_owner_user_id", table_name="data_sources")
    if is_postgres:
        op.drop_constraint(
            "catalog_sync_sequence_nonnegative",
            "data_sources",
            type_="check",
        )
    else:
        with op.batch_alter_table("data_sources") as batch_op:
            batch_op.drop_constraint("catalog_sync_sequence_nonnegative", type_="check")
    op.drop_column("data_sources", "catalog_sync_sequence")

    if is_postgres:
        sync_status, key_kind, foreign_key_target_status = _types()
        foreign_key_target_status.drop(bind, checkfirst=True)
        key_kind.drop(bind, checkfirst=True)
        sync_status.drop(bind, checkfirst=True)
