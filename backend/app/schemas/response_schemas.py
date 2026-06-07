from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.dataset import DatasetStatus, FileType


class UploadAck(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dataset_id: uuid.UUID = Field(validation_alias="id", description="Stable handle for this dataset")
    status: DatasetStatus
    filename: str
    original_name: str
    file_type: FileType
    size_bytes: int


class DatasetStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dataset_id: uuid.UUID = Field(validation_alias="id")
    status: DatasetStatus
    progress_pct: int
    row_count: int | None = None
    col_count: int | None = None
    error_message: str | None = None
    uploaded_at: datetime | None = None
    profiled_at: datetime | None = None


class DatasetSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dataset_id: uuid.UUID = Field(validation_alias="id")
    original_name: str
    file_type: FileType
    size_bytes: int
    status: DatasetStatus
    row_count: int | None = None
    col_count: int | None = None
    uploaded_at: datetime | None = None


class DatasetList(BaseModel):
    items: list[DatasetSummary]
    count: int
