"""Immutable structural catalog records for selected PostgreSQL sources.

These models deliberately persist only database structure discovered from an
explicit source scope. They never contain client rows, profile observations,
embeddings, inferred relationships, or semantic/business definitions.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CatalogSyncRunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CatalogKeyKind(str, enum.Enum):
    PRIMARY_KEY = "PRIMARY_KEY"
    UNIQUE = "UNIQUE"


class CatalogForeignKeyTargetStatus(str, enum.Enum):
    IN_SCOPE = "IN_SCOPE"
    TARGET_OUT_OF_SCOPE = "TARGET_OUT_OF_SCOPE"


class CatalogSyncRun(Base, TimestampMixin):
    """One requested structural synchronization for one creator-owned source."""

    __tablename__ = "catalog_sync_runs"
    __table_args__ = (
        CheckConstraint("request_sequence > 0", name="sequence_positive"),
        CheckConstraint("scope_count > 0", name="scope_count_positive"),
        UniqueConstraint("source_id", "request_sequence", name="uq_catalog_sync_runs_source_sequence"),
        # This exact target key lets a snapshot prove that its source, run,
        # and captured scope fingerprint all agree at the database boundary.
        UniqueConstraint(
            "source_id",
            "id",
            "scope_fingerprint",
            name="uq_catalog_sync_runs_source_id_id_scope_fingerprint",
        ),
        # A run requester must be the current source owner at creation. The
        # referenced source pair is uniquely indexed on DataSource.
        ForeignKeyConstraint(
            ["source_id", "requesting_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_catalog_sync_runs_source_owner",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    requesting_user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # SHA-256 hex of canonical JSON tuples, never a concatenated dotted name.
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[CatalogSyncRunStatus] = mapped_column(
        Enum(CatalogSyncRunStatus, name="catalog_sync_run_status_enum", native_enum=True),
        nullable=False,
        default=CatalogSyncRunStatus.QUEUED,
        server_default=CatalogSyncRunStatus.QUEUED.value,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Deliberately a bounded safe classification, never a driver error string.
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CatalogSnapshot(Base):
    """A complete, immutable structural observation published by one run."""

    __tablename__ = "catalog_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "sync_run_id", "scope_fingerprint"],
            [
                "catalog_sync_runs.source_id",
                "catalog_sync_runs.id",
                "catalog_sync_runs.scope_fingerprint",
            ],
            name="fk_catalog_snapshots_source_run_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "id", name="uq_catalog_snapshots_source_id_id"),
        UniqueConstraint("sync_run_id", name="uq_catalog_snapshots_sync_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    sync_run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceCatalogHead(Base, TimestampMixin):
    """The one published snapshot currently usable for a source.

    A dedicated head table avoids a circular source/snapshot foreign key while
    the composite FK prevents a source from pointing to another source's
    snapshot.
    """

    __tablename__ = "source_catalog_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_source_catalog_heads_source_snapshot",
            ondelete="CASCADE",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)


class CatalogAsset(Base):
    """One selected PostgreSQL table in one immutable snapshot."""

    __tablename__ = "catalog_assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_catalog_assets_source_snapshot",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "snapshot_id", "id", name="uq_catalog_assets_source_snapshot_id"),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "schema_name",
            "table_name",
            name="uq_catalog_assets_snapshot_table_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(63), nullable=False)
    table_name: Mapped[str] = mapped_column(String(63), nullable=False)
    # Database comments are descriptive source content, never trusted semantic definitions.
    table_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogColumn(Base):
    """An ordered native column definition inside a catalog asset."""

    __tablename__ = "catalog_columns"
    __table_args__ = (
        CheckConstraint("ordinal_position > 0", name="ordinal_positive"),
        ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_columns_source_snapshot_asset",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "id",
            name="uq_catalog_columns_source_snapshot_asset_id",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "column_name",
            name="uq_catalog_columns_asset_name",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "ordinal_position",
            name="uq_catalog_columns_asset_ordinal",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    column_name: Mapped[str] = mapped_column(String(63), nullable=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # ``format_type`` plus raw native fields avoid inventing a cross-database type system.
    formatted_native_type: Mapped[str] = mapped_column(Text, nullable=False)
    native_type_schema: Mapped[str | None] = mapped_column(String(63), nullable=True)
    native_type_name: Mapped[str | None] = mapped_column(String(63), nullable=True)
    native_type_oid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    native_type_modifier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogDeclaredKey(Base):
    """A declared PostgreSQL primary or unique key for one asset."""

    __tablename__ = "catalog_declared_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_declared_keys_source_snapshot_asset",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "id",
            name="uq_catalog_declared_keys_source_snapshot_asset_id",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "asset_id",
            "constraint_name",
            name="uq_catalog_declared_keys_asset_constraint",
        ),
        Index(
            "uq_catalog_declared_keys_primary_per_asset",
            "source_id",
            "snapshot_id",
            "asset_id",
            unique=True,
            postgresql_where=text("key_kind = 'PRIMARY_KEY'"),
            sqlite_where=text("key_kind = 'PRIMARY_KEY'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    constraint_name: Mapped[str] = mapped_column(String(63), nullable=False)
    key_kind: Mapped[CatalogKeyKind] = mapped_column(
        Enum(CatalogKeyKind, name="catalog_key_kind_enum", native_enum=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogDeclaredKeyColumn(Base):
    """Ordered constituent column of a declared primary or unique key."""

    __tablename__ = "catalog_declared_key_columns"
    __table_args__ = (
        CheckConstraint("ordinal_position > 0", name="ordinal_positive"),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "key_id",
            "ordinal_position",
            name="uq_catalog_declared_key_columns_key_ordinal",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "key_id",
            "column_id",
            name="uq_catalog_declared_key_columns_key_column",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    key_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    ordinal_position: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    column_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)


class CatalogForeignKey(Base):
    """A declared foreign key, optionally redacted at an excluded target."""

    __tablename__ = "catalog_foreign_keys"
    __table_args__ = (
        CheckConstraint(
            "(target_status = 'IN_SCOPE' AND referenced_asset_id IS NOT NULL) "
            "OR (target_status = 'TARGET_OUT_OF_SCOPE' AND referenced_asset_id IS NULL)",
            name="target_status_matches_asset",
        ),
        ForeignKeyConstraint(
            ["source_id", "snapshot_id", "referencing_asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_foreign_keys_referencing_asset",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "snapshot_id", "referenced_asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_catalog_foreign_keys_referenced_asset",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "referencing_asset_id",
            "id",
            name="uq_catalog_foreign_keys_source_snapshot_referencing_id",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "referencing_asset_id",
            "referenced_asset_id",
            "id",
            name="uq_catalog_foreign_keys_source_snapshot_full_id",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "referencing_asset_id",
            "constraint_name",
            name="uq_catalog_foreign_keys_asset_constraint",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    referencing_asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    constraint_name: Mapped[str] = mapped_column(String(63), nullable=False)
    target_status: Mapped[CatalogForeignKeyTargetStatus] = mapped_column(
        Enum(
            CatalogForeignKeyTargetStatus,
            name="catalog_foreign_key_target_status_enum",
            native_enum=True,
        ),
        nullable=False,
    )
    # NULL deliberately means that the referenced asset is outside selected scope.
    referenced_asset_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogForeignKeyColumn(Base):
    """Ordered mapping for an in-scope declared foreign key."""

    __tablename__ = "catalog_foreign_key_columns"
    __table_args__ = (
        CheckConstraint("ordinal_position > 0", name="ordinal_positive"),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "foreign_key_id",
            "ordinal_position",
            name="uq_catalog_foreign_key_columns_fk_ordinal",
        ),
        UniqueConstraint(
            "source_id",
            "snapshot_id",
            "foreign_key_id",
            "referencing_column_id",
            name="uq_catalog_foreign_key_columns_fk_referencing_column",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    foreign_key_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    ordinal_position: Mapped[int] = mapped_column(Integer, primary_key=True)
    referencing_asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    referenced_asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    referencing_column_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    referenced_column_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
