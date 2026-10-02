"""Pilot configuration and real multipart/storage boundary checks."""
from io import BytesIO
import uuid
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, UploadFile
from pydantic import ValidationError

from agentic_engine.tools.uploader.storage import LocalStorage
from app.core.config import Settings
from app.db.models.dataset import DatasetStatus
from app.exceptions.handlers import register_exception_handlers
from app.middlewares.security import RequestSizeLimitMiddleware
from app.services.dataset_service import create_dataset_from_upload


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for name in ("MAX_CONCURRENT_PIPELINES", "MAX_UPLOAD_BYTES", "MAX_REQUEST_BYTES"):
        monkeypatch.delenv(name, raising=False)
    return Settings(
        _env_file=None, api_key="pilot-test-key", database_url="sqlite://",
        data_dir=tmp_path,
    )


def test_pilot_defaults(settings):
    assert settings.max_concurrent_pipelines == 1
    assert settings.max_upload_bytes == 20971520
    assert settings.max_request_bytes == 22020096


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_concurrency_requires_positive_integer(monkeypatch, value):
    monkeypatch.setenv("MAX_CONCURRENT_PIPELINES", value)
    with pytest.raises(ValidationError, match="max_concurrent_pipelines"):
        Settings(_env_file=None, api_key="pilot-test-key", database_url="sqlite://")


def test_environment_can_override_concurrency(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_PIPELINES", "2")
    assert Settings(
        _env_file=None, api_key="pilot-test-key", database_url="sqlite://",
    ).max_concurrent_pipelines == 2


@pytest.mark.parametrize("field", ["max_upload_bytes", "max_request_bytes"])
def test_size_settings_require_positive_values(settings, field):
    values = settings.model_dump()
    values[field] = 0
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize("extra", [0, 1])
async def test_multipart_file_boundary(settings, extra):
    session = Mock(commit=AsyncMock(), refresh=AsyncMock())
    storage = LocalStorage(settings.raw_dir)
    application = FastAPI()
    application.add_middleware(
        RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes,
    )
    register_exception_handlers(application)

    @application.post("/upload")
    async def upload_file(file: UploadFile):
        record = await create_dataset_from_upload(
            upload=file, session=session, storage=storage, settings=settings,
        )
        return {"size_bytes": record.size_bytes, "status": record.status}

    # Long but valid one-row CSV: exercise the exact file boundary, not row cap.
    data = BytesIO(b"column\n" + b"x" * (settings.max_upload_bytes - 7 + extra))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.post("/upload", files={"file": ("data.csv", data, "text/csv")})
    if extra:
        assert response.status_code == 413
        assert "Pilot version currently supports datasets up to 20 MB." in response.text
        session.add.assert_not_called()
        session.commit.assert_not_awaited()
        assert list(settings.raw_dir.iterdir()) == []
    else:
        assert response.status_code == 200
        assert response.json() == {"size_bytes": 20971520, "status": DatasetStatus.QUEUED}
        session.commit.assert_awaited_once()
        assert next(settings.raw_dir.iterdir()).stat().st_size == 20971520


async def test_request_cap_rejects_before_handler(settings):
    application = FastAPI()
    handler = Mock()
    application.add_middleware(
        RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes,
    )

    @application.post("/upload")
    async def upload_file():
        handler()
        return {}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.post(
            "/upload", content=b"", headers={"Content-Length": str(22020096 + 1)},
        )
    assert response.status_code == 413
    assert response.json()["max_bytes"] == 22020096
    assert "request body exceeds" in response.json()["message"]
    handler.assert_not_called()


async def test_jwt_upload_owner_is_present_at_the_first_commit(settings):
    """A private upload must never be committed as a legacy/null-owned row."""

    class RecordingSession:
        def __init__(self):
            self.record = None
            self.owners_at_commit = []

        def add(self, record):
            self.record = record

        async def commit(self):
            self.owners_at_commit.append(self.record.user_id)

        async def refresh(self, _record):
            pass

    owner_id = uuid.uuid4()
    session = RecordingSession()
    upload = UploadFile(
        filename="private.csv",
        file=BytesIO(b"value\n1\n"),
    )

    record = await create_dataset_from_upload(
        upload=upload,
        session=session,
        storage=LocalStorage(settings.raw_dir),
        settings=settings,
        owner_user_id=owner_id,
    )

    assert record.user_id == owner_id
    assert session.owners_at_commit == [owner_id]
