"""Minimal, redacted provenance for controlled PostgreSQL query execution.

Rows, bound values, SQL text, credentials, host details, and driver messages
are intentionally absent.  This table records only enough lifecycle state to
avoid treating an ambiguous remote operation as safely repeatable.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKeyConstraint, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class PostgresQueryRunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PostgresQueryRun(Base, TimestampMixin):
    """One internal Stage 4B execution attempt.

    A RUNNING record may be ambiguous after a process crash or an application
    persistence failure.  It is deliberately not a queue and must never cause
    automatic re-execution.
    """

    __tablename__ = "postgres_query_runs"
    __table_args__ = (
        CheckConstraint("returned_row_count >= 0", name="returned_row_count_nonnegative"),
        ForeignKeyConstraint(
            ["source_id", "owner_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_postgres_query_runs_source_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_postgres_query_runs_source_snapshot",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_postgres_query_runs_source_snapshot_asset",
            ondelete="CASCADE",
        ),
        # Retain a run if an exceptional version cleanup occurs: only the
        # optional metric reference is nulled, never the non-null source ID.
        ForeignKeyConstraint(
            ["metric_definition_version_id"],
            ["metric_definition_versions.id"],
            name="fk_postgres_query_runs_metric_definition_version",
            ondelete="SET NULL",
        ),
        # A metric version is source-local. The companion composite reference
        # prevents a caller from attaching a valid UUID belonging to another
        # owner's source; MATCH SIMPLE permits ordinary generic-query NULL.
        # Leave this NO ACTION rather than SET NULL because source_id is
        # deliberately non-null; the simple FK above clears the nullable leg.
        ForeignKeyConstraint(
            ["source_id", "metric_definition_version_id"],
            ["metric_definition_versions.source_id", "metric_definition_versions.id"],
            name="fk_postgres_query_runs_metric_definition_version_source",
        ),
        # Stage 5B applies the same source-local provenance rule to a
        # relationship version.  A valid UUID from another source is never a
        # valid execution reference for this run.
        ForeignKeyConstraint(
            ["relationship_definition_version_id"],
            ["relationship_definition_versions.id"],
            name="fk_postgres_query_runs_relationship_definition_version",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["source_id", "relationship_definition_version_id"],
            ["relationship_definition_versions.source_id", "relationship_definition_versions.id"],
            name="fk_postgres_query_runs_relationship_definition_version_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Stage 4A's structural fingerprint; never a SQL or parameter fingerprint.
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[PostgresQueryRunStatus] = mapped_column(
        Enum(PostgresQueryRunStatus, name="postgres_query_run_status_enum", native_enum=True),
        nullable=False,
        default=PostgresQueryRunStatus.RUNNING,
        server_default=PostgresQueryRunStatus.RUNNING.value,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A bounded, documented category only; never raw driver/SQL text.
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    returned_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optional Stage 5A semantic provenance. A plain structural query has no
    # metric version; retiring a metric never deletes its historical version.
    metric_definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    # Optional Stage 5B join provenance.  A generic/single-table plan leaves
    # this unset; the database constraints above bind a populated value to
    # this run's immutable source identity.
    relationship_definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
