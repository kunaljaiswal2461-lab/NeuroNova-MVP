"""Owner-confirmed, versioned same-source PostgreSQL relationships.

The catalog records declared foreign keys and keys, but catalog observations
are immutable and snapshot-scoped.  This module records the owner's explicit
approval of one *bounded* base-to-lookup relationship without treating an
inferred or similarly named pair of columns as executable evidence.

Only ``app.services.relationship_service`` may rematerialize an approved
version against the current selected catalog.  The JSON contract contains
structural fingerprints and ordered mappings, never SQL, credentials, rows,
or arbitrary join expressions.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class RelationshipLifecycleState(str, enum.Enum):
    """Durable lifecycle for a source-local approved relationship."""

    ACTIVE = "ACTIVE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RETIRED = "RETIRED"


class RelationshipJoinType(str, enum.Enum):
    """The deliberately narrow join vocabulary for Stage 5B."""

    INNER = "INNER"
    LEFT = "LEFT"


class RelationshipDefinition(Base, TimestampMixin):
    """One logical approved base-to-lookup relationship within one source."""

    __tablename__ = "relationship_definitions"
    __table_args__ = (
        CheckConstraint("current_version_number > 0", name="current_version_positive"),
        ForeignKeyConstraint(
            ["source_id", "owner_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_relationship_definitions_source_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "id", name="uq_relationship_definitions_source_id_id"),
        # The hash is server-derived from the exact base/lookup table
        # identities and declared FK name.  It prevents two active approvals
        # of the same declared direction while allowing a retired historical
        # approval to remain readable.
        Index(
            "uq_relationship_definitions_source_active_identity",
            "source_id",
            "identity_fingerprint",
            unique=True,
            postgresql_where=text("lifecycle_state = 'ACTIVE'"),
            sqlite_where=text("lifecycle_state = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    # Server-generated presentation label and stable, non-semantic identity.
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_state: Mapped[RelationshipLifecycleState] = mapped_column(
        Enum(
            RelationshipLifecycleState,
            name="relationship_lifecycle_state_enum",
            native_enum=True,
        ),
        nullable=False,
        default=RelationshipLifecycleState.ACTIVE,
        server_default=RelationshipLifecycleState.ACTIVE.value,
    )
    current_version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    needs_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bounded structural category only; it must not hold catalog comments,
    # database errors, or client data.
    needs_review_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class RelationshipDefinitionVersion(Base):
    """One immutable owner-confirmed declared-FK relationship contract."""

    __tablename__ = "relationship_definition_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_positive"),
        CheckConstraint("base_asset_id <> lookup_asset_id", name="base_and_lookup_are_distinct"),
        ForeignKeyConstraint(
            ["source_id", "relationship_definition_id"],
            ["relationship_definitions.source_id", "relationship_definitions.id"],
            name="fk_relationship_definition_versions_definition_source",
            ondelete="CASCADE",
        ),
        # This proves that the initially confirmed FK was an in-scope,
        # source-local catalog record with the same base and lookup assets.
        ForeignKeyConstraint(
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
        # A join direction is approved only when its lookup target has a
        # declared primary/unique key matching the ordered FK target columns.
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["source_id", "confirmed_by_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_relationship_definition_versions_confirming_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "relationship_definition_id",
            "version_number",
            name="uq_relationship_definition_versions_number",
        ),
        # Future query-run provenance uses this source-safe target rather
        # than trusting a globally valid UUID.
        UniqueConstraint(
            "source_id", "id", name="uq_relationship_definition_versions_source_id_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    relationship_definition_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    foreign_key_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    base_asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    lookup_asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    target_key_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    join_type: Mapped[RelationshipJoinType] = mapped_column(
        Enum(RelationshipJoinType, name="relationship_join_type_enum", native_enum=True),
        nullable=False,
    )
    definition_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    confirmed_by_user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "RelationshipDefinition",
    "RelationshipDefinitionVersion",
    "RelationshipJoinType",
    "RelationshipLifecycleState",
]
