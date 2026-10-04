"""Owner-confirmed, versioned single-table PostgreSQL metric definitions.

These records intentionally store a compact, typed definition contract rather
than SQL, formulas, result rows, credentials, or a copied catalog snapshot.
Every executable use is revalidated against the source's *current* catalog by
``app.services.metric_service`` before it expands into Stage 4A's existing
``PostgresQueryPlan``.
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


class MetricLifecycleState(str, enum.Enum):
    """Durable lifecycle of a logical metric identity."""

    ACTIVE = "ACTIVE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RETIRED = "RETIRED"


class MetricAggregateOperation(str, enum.Enum):
    """The deliberately small aggregate vocabulary confirmed by an owner."""

    COUNT = "COUNT"
    SUM = "SUM"
    AVERAGE = "AVERAGE"
    MINIMUM = "MINIMUM"
    MAXIMUM = "MAXIMUM"


class MetricDefinition(Base, TimestampMixin):
    """One logical source-local metric, whose current definition is versioned."""

    __tablename__ = "metric_definitions"
    __table_args__ = (
        CheckConstraint("current_version_number > 0", name="current_version_positive"),
        ForeignKeyConstraint(
            ["source_id", "owner_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_metric_definitions_source_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "id", name="uq_metric_definitions_source_id_id"),
        # Prevent two currently usable definitions from becoming ambiguous in
        # one source. A retired or review-blocked historical name may be
        # explicitly reconfirmed as a new active metric.
        Index(
            "uq_metric_definitions_source_active_normalized_name",
            "source_id",
            "normalized_name",
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
    # Current display values mirror the current immutable version for fast,
    # safe matching. Older spelling/description remains on version rows.
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    lifecycle_state: Mapped[MetricLifecycleState] = mapped_column(
        Enum(MetricLifecycleState, name="metric_lifecycle_state_enum", native_enum=True),
        nullable=False,
        default=MetricLifecycleState.ACTIVE,
        server_default=MetricLifecycleState.ACTIVE.value,
    )
    current_version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    needs_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A bounded code, never a database error, column comment, filter value,
    # or customer data.
    needs_review_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class MetricDefinitionVersion(Base):
    """Immutable, owner-confirmed contract for one metric version.

    ``definition_json`` holds only canonical typed aggregate/filter/default
    data plus the small structural fingerprints required to detect a renamed,
    removed, retyped, nullable, or out-of-scope current catalog column.  It
    is not an audit log and must never be written to ordinary logs.
    """

    __tablename__ = "metric_definition_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_positive"),
        # COUNT may intentionally mean COUNT(*) (no column) or COUNT(column)
        # (non-null values only); every other supported aggregate needs one.
        CheckConstraint(
            "aggregate_operation = 'COUNT' OR aggregate_column_id IS NOT NULL",
            name="aggregate_column_matches_operation",
        ),
        ForeignKeyConstraint(
            ["source_id", "metric_definition_id"],
            ["metric_definitions.source_id", "metric_definitions.id"],
            name="fk_metric_definition_versions_definition_source",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "confirmed_snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_metric_definition_versions_catalog_asset",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "confirmed_by_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_metric_definition_versions_confirming_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint("metric_definition_id", "version_number", name="uq_metric_definition_versions_number"),
        UniqueConstraint("source_id", "id", name="uq_metric_definition_versions_source_id_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    metric_definition_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    aggregate_operation: Mapped[MetricAggregateOperation] = mapped_column(
        Enum(MetricAggregateOperation, name="metric_aggregate_operation_enum", native_enum=True),
        nullable=False,
    )
    aggregate_column_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    unit_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_date_column_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    default_time_zone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Keep SQLite fixtures portable while using JSONB in PostgreSQL.
    definition_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    confirmed_by_user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "MetricAggregateOperation",
    "MetricDefinition",
    "MetricDefinitionVersion",
    "MetricLifecycleState",
]
