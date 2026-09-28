from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.core.config import Settings
from app.db.models.dataset import DatasetStatus
from app.exceptions.custom_exceptions import ConflictError
from app.services.account_service import delete_account
from agentic_engine.tools.uploader.storage import LocalStorage


class _Result:
    def __init__(self, datasets):
        self._datasets = datasets

    def scalars(self):
        return SimpleNamespace(all=lambda: self._datasets)


class _Session:
    def __init__(self, datasets):
        self.datasets = datasets
        self.deleted = []
        self.executed = []
        self.did_flush = False
        self.did_commit = False
        self.did_rollback = False

    async def execute(self, statement):
        self.executed.append(statement)
        return _Result(self.datasets)

    async def delete(self, record):
        self.deleted.append(record)

    async def flush(self):
        self.did_flush = True

    async def commit(self):
        self.did_commit = True

    async def rollback(self):
        self.did_rollback = True


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        api_key="test-api-key-12345",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        openai_api_key="",
        data_dir=tmp_path,
    )
    settings.ensure_data_dirs()
    return settings


@pytest.mark.asyncio
async def test_delete_account_removes_owned_dataset_artifacts_and_user(tmp_path):
    settings = _settings(tmp_path)
    user = SimpleNamespace(id=uuid.uuid4())
    dataset = SimpleNamespace(
        id=uuid.uuid4(),
        original_name="sales.csv",
        status=DatasetStatus.COMPLETE,
    )
    raw_path = settings.raw_dir / f"{dataset.id}_sales.csv"
    raw_path.write_text("revenue\n100\n", encoding="utf-8")
    artifact_paths = [
        directory / f"{dataset.id}.json"
        for directory in (
            settings.profiles_dir,
            settings.findings_dir,
            settings.viz_dir,
            settings.llm_cache_dir,
        )
    ]
    for path in artifact_paths:
        path.write_text("{}", encoding="utf-8")

    session = _Session([dataset])
    await delete_account(
        session=session,
        storage=LocalStorage(settings.raw_dir),
        settings=settings,
        user=user,
    )

    assert not raw_path.exists()
    assert all(not path.exists() for path in artifact_paths)
    assert session.deleted == [dataset, user]
    assert session.did_flush is True
    assert session.did_commit is True
    assert session.did_rollback is False
    assert len(session.executed) == 2


@pytest.mark.asyncio
async def test_delete_account_rejects_while_a_dataset_is_processing(tmp_path):
    settings = _settings(tmp_path)
    user = SimpleNamespace(id=uuid.uuid4())
    dataset = SimpleNamespace(
        id=uuid.uuid4(),
        original_name="sales.csv",
        status=DatasetStatus.PROFILING,
    )
    session = _Session([dataset])

    with pytest.raises(ConflictError, match="wait for dataset processing"):
        await delete_account(
            session=session,
            storage=LocalStorage(settings.raw_dir),
            settings=settings,
            user=user,
        )

    assert session.deleted == []
    assert session.did_commit is False
