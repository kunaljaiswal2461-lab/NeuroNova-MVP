from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.dataset import DatasetRecord, DatasetStatus, FileType
from app.exceptions.custom_exceptions import (
    FileTooLargeError,
    NotFoundError,
    UnsupportedFileTypeError,
    ValidationFailure,
)
from agentic_engine.tools.uploader.storage import (
    FileTooLargeError as StorageFileTooLargeError,
    StorageBackend,
)


logger = get_logger("dataset_service")

# Map of allowed extension → declared FileType + accepted content-types.
# We trust extension as the primary signal (content-type detection on Windows
# without libmagic is flaky); content-type is validated as a soft check.
EXTENSION_TO_FILE_TYPE: dict[str, FileType] = {
    ".csv": FileType.CSV,
    ".tsv": FileType.CSV,
    ".xlsx": FileType.XLSX,
    ".xls": FileType.XLSX,
    ".json": FileType.JSON,
    ".jsonl": FileType.JSON,
    ".ndjson": FileType.JSON,
    ".parquet": FileType.PARQUET,
    ".pq": FileType.PARQUET,
}

ALLOWED_CONTENT_TYPES: set[str] = {
    "text/csv",
    "text/plain",
    "text/tab-separated-values",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/json",
    "application/x-ndjson",
    "application/octet-stream",  # parquet, generic
}

UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB


def _detect_file_type(filename: str, content_type: str | None) -> FileType:
    ext = Path(filename).suffix.lower()
    if ext not in EXTENSION_TO_FILE_TYPE:
        raise UnsupportedFileTypeError(
            f"unsupported file extension: {ext or '(none)'}",
            details={"supported": sorted(EXTENSION_TO_FILE_TYPE)},
        )
    if content_type and content_type not in ALLOWED_CONTENT_TYPES:
        # Soft warning — many clients send octet-stream for everything.
        logger.warning(
            "unexpected_content_type",
            content_type=content_type,
            filename=filename,
        )
    return EXTENSION_TO_FILE_TYPE[ext]


async def _stream_upload(upload: UploadFile) -> AsyncIterator[bytes]:
    while True:
        chunk = await upload.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        yield chunk


async def create_dataset_from_upload(
    *,
    upload: UploadFile,
    session: AsyncSession,
    storage: StorageBackend,
    settings: Settings,
) -> DatasetRecord:
    if upload.filename is None or upload.filename.strip() == "":
        raise ValidationFailure("filename is required")

    original_name = upload.filename
    file_type = _detect_file_type(original_name, upload.content_type)

    dataset_id = uuid.uuid4()
    safe_name = Path(original_name).name

    try:
        target_path, size_bytes = await storage.save_stream(
            file_id=str(dataset_id),
            filename=safe_name,
            stream=_stream_upload(upload),
            max_bytes=settings.max_upload_bytes,
        )
    except StorageFileTooLargeError as exc:
        raise FileTooLargeError(
            f"file exceeds upload limit of {exc.limit_bytes} bytes",
            details={"max_bytes": exc.limit_bytes},
        ) from exc

    if size_bytes == 0:
        await storage.delete(str(dataset_id), safe_name)
        raise ValidationFailure("uploaded file is empty")

    record = DatasetRecord(
        id=dataset_id,
        filename=target_path.name,
        original_name=original_name,
        file_type=file_type,
        size_bytes=size_bytes,
        status=DatasetStatus.QUEUED,
        progress_pct=0,
        uploaded_at=datetime.now(timezone.utc),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    logger.info(
        "dataset.uploaded",
        dataset_id=str(dataset_id),
        original_name=original_name,
        file_type=file_type.value,
        size_bytes=size_bytes,
    )
    return record


async def get_dataset(session: AsyncSession, dataset_id: uuid.UUID) -> DatasetRecord:
    record = await session.get(DatasetRecord, dataset_id)
    if record is None:
        raise NotFoundError(
            f"dataset {dataset_id} not found",
            details={"dataset_id": str(dataset_id)},
        )
    return record


async def list_datasets(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[DatasetRecord], int]:
    stmt = (
        select(DatasetRecord)
        .order_by(DatasetRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    items = list(result.scalars().all())

    count_stmt = select(DatasetRecord.id)
    count_result = await session.execute(count_stmt)
    total = len(count_result.all())

    return items, total


async def mark_status(
    session: AsyncSession,
    dataset_id: uuid.UUID,
    *,
    status: DatasetStatus,
    progress_pct: int | None = None,
    error_message: str | None = None,
) -> DatasetRecord:
    record = await get_dataset(session, dataset_id)
    record.status = status
    if progress_pct is not None:
        record.progress_pct = progress_pct
    if status == DatasetStatus.COMPLETE:
        record.profiled_at = datetime.now(timezone.utc)
        record.progress_pct = 100
    if status == DatasetStatus.FAILED and error_message is not None:
        record.error_message = error_message
    await session.commit()
    await session.refresh(record)
    return record
