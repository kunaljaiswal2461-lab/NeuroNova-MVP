"""DatasetSummary must preserve processing fields so the upload list can show
interrupted jobs with their progress and message (previously dropped)."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.db.models.dataset import DatasetStatus, FileType
from app.schemas.response_schemas import DatasetSummary


FAILED = SimpleNamespace(
    id=uuid.uuid4(),
    original_name="interrupted.csv",
    file_type=FileType.CSV,
    size_bytes=1234,
    status=DatasetStatus.FAILED,
    progress_pct=30,
    error_message="Processing was interrupted. Please upload the dataset again.",
    row_count=None,
    col_count=None,
    uploaded_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
)


def test_dataset_summary_preserves_error_and_progress():
    parsed = DatasetSummary.model_validate(FAILED)
    assert parsed.dataset_id == FAILED.id
    assert parsed.status is DatasetStatus.FAILED
    assert parsed.progress_pct == 30
    assert parsed.error_message == "Processing was interrupted. Please upload the dataset again."


def test_dataset_summary_defaults_processing_fields_when_absent():
    legacy = SimpleNamespace(
        id=uuid.uuid4(),
        original_name="old.csv",
        file_type=FileType.CSV,
        size_bytes=1,
        status=DatasetStatus.COMPLETE,
        row_count=None,
        col_count=None,
        uploaded_at=None,
    )
    parsed = DatasetSummary.model_validate(legacy)
    assert parsed.progress_pct == 0
    assert parsed.error_message is None
