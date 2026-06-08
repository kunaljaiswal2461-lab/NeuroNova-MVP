from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class FileType(str, enum.Enum):
    CSV = "CSV"
    XLSX = "XLSX"
    JSON = "JSON"
    PARQUET = "PARQUET"


class DatasetStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PROFILING = "PROFILING"
    FINDINGS = "FINDINGS"
    VIZ = "VIZ"
    INSIGHTS = "INSIGHTS"
    INDEXING = "INDEXING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class DatasetRecord(Base, TimestampMixin):
    __tablename__ = "dataset_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)

    file_type: Mapped[FileType] = mapped_column(
        Enum(FileType, name="file_type_enum", native_enum=True),
        nullable=False,
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    col_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[DatasetStatus] = mapped_column(
        Enum(DatasetStatus, name="dataset_status_enum", native_enum=True),
        nullable=False,
        default=DatasetStatus.UPLOADED,
    )

    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    profiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
