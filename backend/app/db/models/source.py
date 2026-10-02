"""Persisted external PostgreSQL source ownership and encrypted credentials.

The three tables in this module intentionally stop at onboarding. They do not
contain catalog snapshots, profile observations, semantic definitions, query
runs, or client data.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class SourceType(str, enum.Enum):
    POSTGRESQL = "POSTGRESQL"


class SourceStatus(str, enum.Enum):
    # A successful test/save observation. It does not claim that a catalog has
    # been built or that the remote database will stay reachable.
    TESTED = "TESTED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"


class TLSMode(str, enum.Enum):
    VERIFY_FULL = "VERIFY_FULL"


class DataSource(Base, TimestampMixin):
    __tablename__ = "data_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type_enum", native_enum=True),
        nullable=False,
        default=SourceType.POSTGRESQL,
        server_default=SourceType.POSTGRESQL.value,
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5432)
    database_name: Mapped[str] = mapped_column(String(63), nullable=False)
    tls_mode: Mapped[TLSMode] = mapped_column(
        Enum(TLSMode, name="source_tls_mode_enum", native_enum=True),
        nullable=False,
        default=TLSMode.VERIFY_FULL,
        server_default=TLSMode.VERIFY_FULL.value,
    )
    has_custom_ca: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[SourceStatus] = mapped_column(
        Enum(SourceStatus, name="source_status_enum", native_enum=True),
        nullable=False,
        default=SourceStatus.TESTED,
        server_default=SourceStatus.TESTED.value,
    )
    # Safe classification only; no server, role, or driver exception text.
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stored now for the later profiling stage; it cannot be enabled in 2B.
    profiling_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    credential: Mapped["SourceCredential | None"] = relationship(
        back_populates="source",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    table_scopes: Mapped[list["SourceTableScope"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SourceCredential(Base, TimestampMixin):
    """One encrypted credential bundle per source; never serialize this model."""

    __tablename__ = "source_credentials"

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    encryption_format: Mapped[str] = mapped_column(String(32), nullable=False)

    source: Mapped[DataSource] = relationship(back_populates="credential")


class SourceTableScope(Base):
    """Explicit current table selection; no schema wildcard is represented."""

    __tablename__ = "source_table_scopes"

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    schema_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    table_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source: Mapped[DataSource] = relationship(back_populates="table_scopes")
