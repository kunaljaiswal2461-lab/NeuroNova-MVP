"""Durable, aggregate-only observations from optional PostgreSQL profiling.

These rows are deliberately separate from catalog structure, semantic
definitions, and file-pipeline profile reports.  They retain no sampled rows,
values, SQL text, credentials, or driver messages.  A profile always describes
only the bounded observed population recorded on its run and observations.
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
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SourceProfileRunStatus(str, enum.Enum):
    """Lifecycle of a durable, manually requested profile job."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SourceProfileSamplingMethod(str, enum.Enum):
    """The initial bounded population construction strategy."""

    SYSTEM_TABLE_SAMPLE = "SYSTEM_TABLE_SAMPLE"


class SourceProfilePopulation(str, enum.Enum):
    """What an observation truthfully describes."""

    OBSERVED_SAMPLE = "OBSERVED_SAMPLE"


class SourceProfileObservationStatus(str, enum.Enum):
    """A complete observation is still only complete for its sample."""

    COMPLETE = "COMPLETE"
    EMPTY_SAMPLE = "EMPTY_SAMPLE"
    UNSUPPORTED = "UNSUPPORTED"
    INCOMPLETE = "INCOMPLETE"


class SourceProfileFindingType(str, enum.Enum):
    OBSERVED_NULL_RATE = "OBSERVED_NULL_RATE"
    OBSERVED_CONSTANT_NON_NULL = "OBSERVED_CONSTANT_NON_NULL"


class SourceProfileFindingSeverity(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"


class SourceProfileRun(Base, TimestampMixin):
    """One owner-requested bounded profile against one current catalog head.

    The run captures the active source scope and selected catalog identities at
    request time.  Lease data fences independently supervised workers; it is
    intentionally never exposed through the public API.
    """

    __tablename__ = "source_profile_runs"
    __table_args__ = (
        CheckConstraint("scope_count > 0", name="scope_count_positive"),
        CheckConstraint("selected_asset_count > 0", name="selected_asset_count_positive"),
        CheckConstraint("selected_column_count >= 0", name="selected_column_count_nonnegative"),
        CheckConstraint("sample_percent > 0 AND sample_percent <= 10", name="sample_percent_bounded"),
        CheckConstraint("sample_limit > 0", name="sample_limit_positive"),
        CheckConstraint("sample_seed >= 0", name="sample_seed_nonnegative"),
        CheckConstraint("max_operations > 0", name="max_operations_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("config_generation >= 0", name="config_generation_nonnegative"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_fields_match",
        ),
        ForeignKeyConstraint(
            ["source_id", "requesting_user_id"],
            ["data_sources.id", "data_sources.owner_user_id"],
            name="fk_source_profile_runs_source_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "snapshot_id"],
            ["catalog_snapshots.source_id", "catalog_snapshots.id"],
            name="fk_source_profile_runs_source_snapshot",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "id", name="uq_source_profile_runs_source_id_id"),
        UniqueConstraint(
            "source_id",
            "id",
            "snapshot_id",
            name="uq_source_profile_runs_source_id_id_snapshot_id",
        ),
        Index("ix_source_profile_runs_source_status_requested", "source_id", "status", "requested_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    requesting_user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    # SHA-256 fingerprints encode structured IDs/current source scope, never
    # customer data, SQL, or untrusted dotted identifiers.
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_asset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_column_count: Mapped[int] = mapped_column(Integer, nullable=False)
    config_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    sampling_method: Mapped[SourceProfileSamplingMethod] = mapped_column(
        Enum(SourceProfileSamplingMethod, name="source_profile_sampling_method_enum", native_enum=True),
        nullable=False,
    )
    sample_percent: Mapped[float] = mapped_column(Numeric(7, 4), nullable=False)
    sample_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # Exact REPEATABLE seed used by the fixed TABLESAMPLE statement. This is
    # sampling provenance, not a user-provided execution parameter.
    sample_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    max_operations: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SourceProfileRunStatus] = mapped_column(
        Enum(SourceProfileRunStatus, name="source_profile_run_status_enum", native_enum=True),
        nullable=False,
        default=SourceProfileRunStatus.QUEUED,
        server_default=SourceProfileRunStatus.QUEUED.value,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Safe bounded category only; never a remote message or query text.
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceProfileRunAsset(Base):
    """One current-catalog table explicitly selected for a profile run."""

    __tablename__ = "source_profile_run_assets"
    __table_args__ = (
        CheckConstraint("selected_column_count >= 0", name="selected_column_count_nonnegative"),
        ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id"],
            [
                "source_profile_runs.source_id",
                "source_profile_runs.id",
                "source_profile_runs.snapshot_id",
            ],
            name="fk_source_profile_run_assets_run_snapshot",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "snapshot_id", "asset_id"],
            ["catalog_assets.source_id", "catalog_assets.snapshot_id", "catalog_assets.id"],
            name="fk_source_profile_run_assets_catalog_asset",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "run_id", "asset_id", name="uq_source_profile_run_assets_run_asset"),
        UniqueConstraint(
            "source_id", "run_id", "snapshot_id", "asset_id", name="uq_source_profile_run_assets_full"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    selected_column_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SourceProfileRunColumn(Base):
    """One supported scalar column explicitly selected for a profile run."""

    __tablename__ = "source_profile_run_columns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id"],
            [
                "source_profile_run_assets.source_id",
                "source_profile_run_assets.run_id",
                "source_profile_run_assets.snapshot_id",
                "source_profile_run_assets.asset_id",
            ],
            name="fk_source_profile_run_columns_run_asset",
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
            name="fk_source_profile_run_columns_catalog_column",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "run_id", "column_id", name="uq_source_profile_run_columns_run_column"),
        UniqueConstraint(
            "source_id", "run_id", "snapshot_id", "asset_id", "column_id",
            name="uq_source_profile_run_columns_full",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    column_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # A small internal category supports deterministic operation selection. It
    # is not a semantic/business inference and never contains customer data.
    scalar_kind: Mapped[str] = mapped_column(String(32), nullable=False)


class SourceProfileTableObservation(Base):
    """Aggregate-only facts for the single bounded population of one table."""

    __tablename__ = "source_profile_table_observations"
    __table_args__ = (
        CheckConstraint("observed_row_count >= 0", name="observed_row_count_nonnegative"),
        CheckConstraint("sample_percent > 0 AND sample_percent <= 10", name="sample_percent_bounded"),
        CheckConstraint("sample_limit > 0", name="sample_limit_positive"),
        CheckConstraint("sample_seed >= 0", name="sample_seed_nonnegative"),
        ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id"],
            [
                "source_profile_run_assets.source_id",
                "source_profile_run_assets.run_id",
                "source_profile_run_assets.snapshot_id",
                "source_profile_run_assets.asset_id",
            ],
            name="fk_source_profile_table_observations_run_asset",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "run_id", "asset_id", name="uq_source_profile_table_observations_run_asset"),
        UniqueConstraint("source_id", "id", name="uq_source_profile_table_observations_source_id_id"),
        # These composite identities let downstream artifacts prove that a
        # table observation belongs to this exact run/snapshot/asset, rather
        # than merely to the same source.
        UniqueConstraint(
            "source_id",
            "run_id",
            "id",
            name="uq_source_profile_table_observations_source_run_id",
        ),
        UniqueConstraint(
            "source_id",
            "run_id",
            "snapshot_id",
            "asset_id",
            "id",
            name="uq_source_profile_table_observations_provenance",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sampling_method: Mapped[SourceProfileSamplingMethod] = mapped_column(
        Enum(SourceProfileSamplingMethod, name="source_profile_observation_sampling_method_enum", native_enum=True),
        nullable=False,
    )
    sample_percent: Mapped[float] = mapped_column(Numeric(7, 4), nullable=False)
    sample_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    population: Mapped[SourceProfilePopulation] = mapped_column(
        Enum(SourceProfilePopulation, name="source_profile_population_enum", native_enum=True),
        nullable=False,
        default=SourceProfilePopulation.OBSERVED_SAMPLE,
    )
    observed_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SourceProfileObservationStatus] = mapped_column(
        Enum(
            SourceProfileObservationStatus,
            name="source_profile_observation_status_enum",
            native_enum=True,
        ),
        nullable=False,
    )
    # A bounded, code-only list such as ["sample_empty"]. It must never hold
    # values, exception messages, SQL, or opaque remote server data.
    warning_codes: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )


class SourceProfileColumnObservation(Base):
    """Aggregate-only column facts tied to the table observation's population."""

    __tablename__ = "source_profile_column_observations"
    __table_args__ = (
        CheckConstraint("observed_row_count >= 0", name="observed_row_count_nonnegative"),
        CheckConstraint("null_count >= 0", name="null_count_nonnegative"),
        CheckConstraint("non_null_count >= 0", name="non_null_count_nonnegative"),
        CheckConstraint("distinct_count >= 0", name="distinct_count_nonnegative"),
        CheckConstraint("distinct_count <= non_null_count", name="distinct_count_within_non_null"),
        CheckConstraint("null_count + non_null_count = observed_row_count", name="counts_match_population"),
        ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id", "column_id"],
            [
                "source_profile_run_columns.source_id",
                "source_profile_run_columns.run_id",
                "source_profile_run_columns.snapshot_id",
                "source_profile_run_columns.asset_id",
                "source_profile_run_columns.column_id",
            ],
            name="fk_source_profile_column_observations_run_column",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "run_id", "snapshot_id", "asset_id", "table_observation_id"],
            [
                "source_profile_table_observations.source_id",
                "source_profile_table_observations.run_id",
                "source_profile_table_observations.snapshot_id",
                "source_profile_table_observations.asset_id",
                "source_profile_table_observations.id",
            ],
            name="fk_source_profile_column_observations_table_provenance",
            ondelete="CASCADE",
        ),
        UniqueConstraint("source_id", "run_id", "column_id", name="uq_source_profile_column_observations_run_column"),
        UniqueConstraint("source_id", "id", name="uq_source_profile_column_observations_source_id_id"),
        UniqueConstraint(
            "source_id",
            "run_id",
            "table_observation_id",
            "id",
            name="uq_source_profile_column_observations_finding_provenance",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    column_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    table_observation_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    population: Mapped[SourceProfilePopulation] = mapped_column(
        Enum(SourceProfilePopulation, name="source_profile_column_population_enum", native_enum=True),
        nullable=False,
        default=SourceProfilePopulation.OBSERVED_SAMPLE,
    )
    observed_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    null_count: Mapped[int] = mapped_column(Integer, nullable=False)
    non_null_count: Mapped[int] = mapped_column(Integer, nullable=False)
    null_rate: Mapped[float | None] = mapped_column(Numeric(9, 8), nullable=True)
    distinct_count: Mapped[int] = mapped_column(Integer, nullable=False)
    numeric_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Numeric extrema are permitted only for numeric scalar columns. They are
    # aggregate values, not raw sampled rows or text extrema.
    numeric_sum: Mapped[float | None] = mapped_column(Numeric(38, 10), nullable=True)
    numeric_minimum: Mapped[float | None] = mapped_column(Numeric(38, 10), nullable=True)
    numeric_maximum: Mapped[float | None] = mapped_column(Numeric(38, 10), nullable=True)
    numeric_mean: Mapped[float | None] = mapped_column(Numeric(38, 10), nullable=True)
    status: Mapped[SourceProfileObservationStatus] = mapped_column(
        Enum(
            SourceProfileObservationStatus,
            name="source_profile_column_observation_status_enum",
            native_enum=True,
        ),
        nullable=False,
    )
    warning_codes: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )


class SourceProfileFinding(Base):
    """A deterministic, sample-qualified finding with normalized evidence."""

    __tablename__ = "source_profile_findings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "run_id"],
            ["source_profile_runs.source_id", "source_profile_runs.id"],
            name="fk_source_profile_findings_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "run_id", "table_observation_id"],
            [
                "source_profile_table_observations.source_id",
                "source_profile_table_observations.run_id",
                "source_profile_table_observations.id",
            ],
            name="fk_source_profile_findings_table_provenance",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "run_id", "table_observation_id", "column_observation_id"],
            [
                "source_profile_column_observations.source_id",
                "source_profile_column_observations.run_id",
                "source_profile_column_observations.table_observation_id",
                "source_profile_column_observations.id",
            ],
            name="fk_source_profile_findings_column_provenance",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "source_id", "run_id", "finding_type", "column_observation_id",
            name="uq_source_profile_findings_run_type_column",
        ),
        Index("ix_source_profile_findings_source_run", "source_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    table_observation_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    column_observation_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    finding_type: Mapped[SourceProfileFindingType] = mapped_column(
        Enum(SourceProfileFindingType, name="source_profile_finding_type_enum", native_enum=True),
        nullable=False,
    )
    severity: Mapped[SourceProfileFindingSeverity] = mapped_column(
        Enum(SourceProfileFindingSeverity, name="source_profile_finding_severity_enum", native_enum=True),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    limitations: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "SourceProfileColumnObservation",
    "SourceProfileFinding",
    "SourceProfileFindingSeverity",
    "SourceProfileFindingType",
    "SourceProfileObservationStatus",
    "SourceProfilePopulation",
    "SourceProfileRun",
    "SourceProfileRunAsset",
    "SourceProfileRunColumn",
    "SourceProfileRunStatus",
    "SourceProfileSamplingMethod",
    "SourceProfileTableObservation",
]
