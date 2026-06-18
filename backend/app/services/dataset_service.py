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

# Hard blocklist of dangerous extensions — even if Windows or a CDN
# remaps the content-type, we reject anything that *looks* executable.
BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    ".exe", ".dll", ".bat", ".cmd", ".com", ".msi", ".scr", ".cpl",
    ".sh", ".bash", ".zsh",
    ".ps1", ".vbs", ".js", ".jse", ".wsf", ".wsh",
    ".py", ".pyc", ".pyo",
    ".jar", ".war", ".apk",
    ".php", ".phtml", ".phar",
    ".rb", ".pl", ".cgi",
    ".html", ".htm", ".svg",
})


def is_allowed_upload(filename: str | None, content_type: str | None) -> bool:
    """Return True iff this filename+content-type passes the upload allowlist.

    Pure predicate so it can be unit-tested without an UploadFile fixture.
    Rejects: missing/empty name, blocked executable extensions, and any
    extension not on EXTENSION_TO_FILE_TYPE.
    """
    if not filename or not filename.strip():
        return False
    ext = Path(filename).suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        return False
    return ext in EXTENSION_TO_FILE_TYPE


def _count_lines_streaming(path: Path, max_lines: int) -> int:
    """Count newline bytes in ``path`` without loading the whole file.

    Returns the first count that exceeds ``max_lines`` (early exit) — for
    text files this approximates row count cheaply. For binary formats
    (XLSX, Parquet) newline counts are meaningless and we skip the check.
    """
    count = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            count += chunk.count(b"\n")
            if count > max_lines:
                return count
    return count

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

    # Explicit MIME/extension allowlist + executable blocklist.
    if not is_allowed_upload(original_name, upload.content_type):
        raise UnsupportedFileTypeError(
            "file type is not accepted — upload CSV, Excel, JSON, or Parquet only",
            details={"supported": sorted(EXTENSION_TO_FILE_TYPE)},
        )

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

    # Cheap row-count guard for text formats. Binary formats (XLSX,
    # Parquet) skip this — for those the row cap is enforced after
    # loading inside the profiler.
    if file_type in (FileType.CSV, FileType.JSON):
        line_count = _count_lines_streaming(target_path, settings.max_upload_rows)
        if line_count > settings.max_upload_rows:
            await storage.delete(str(dataset_id), safe_name)
            raise ValidationFailure(
                f"uploaded file exceeds row limit of {settings.max_upload_rows:,}",
                details={"max_rows": settings.max_upload_rows},
            )

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


async def get_dataset(
    session: AsyncSession,
    dataset_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
) -> DatasetRecord:
    """Fetch a dataset, optionally scoped to a caller's identity.

    When ``user_id`` is provided, the row must match BOTH the dataset id
    and the owning ``user_id``. A mismatch raises ``NotFoundError`` (404,
    never 403) so callers cannot enumerate other tenants' dataset ids.

    Legacy ``X-API-Key`` callers pass ``user_id=None`` and continue to
    see all datasets — they share the static server key.
    """
    if user_id is None:
        record = await session.get(DatasetRecord, dataset_id)
    else:
        stmt = select(DatasetRecord).where(
            DatasetRecord.id == dataset_id,
            DatasetRecord.user_id == user_id,
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
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
    user_id: uuid.UUID | None = None,
) -> tuple[list[DatasetRecord], int]:
    stmt = select(DatasetRecord).order_by(DatasetRecord.created_at.desc())
    count_stmt = select(DatasetRecord.id)

    # When a user is logged in, scope datasets to their ownership.
    # Legacy X-API-Key callers (user_id=None) still see everything for backwards-compat scripts.
    if user_id is not None:
        stmt = stmt.where(DatasetRecord.user_id == user_id)
        count_stmt = count_stmt.where(DatasetRecord.user_id == user_id)

    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    items = list(result.scalars().all())

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
