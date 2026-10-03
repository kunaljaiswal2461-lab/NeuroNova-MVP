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
