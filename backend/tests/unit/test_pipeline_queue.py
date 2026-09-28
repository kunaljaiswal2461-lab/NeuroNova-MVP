"""Exercise the real pipeline semaphore with two jobs and mocked heavy stages."""
import asyncio
import threading
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agentic_engine.workflows import profiling_pipeline as pipeline
from app.db.models.dataset import DatasetStatus, FileType


@pytest.mark.parametrize("first_fails", [False, True])
async def test_second_job_stays_queued_until_first_releases_slot(monkeypatch, tmp_path, first_fails):
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    settings = SimpleNamespace(max_concurrent_pipelines=1, raw_dir=tmp_path)
    states = {first_id: DatasetStatus.QUEUED, second_id: DatasetStatus.QUEUED}
    records = {
        dataset_id: SimpleNamespace(filename=f"{dataset_id}.csv", file_type=FileType.CSV)
        for dataset_id in states
    }
    transitions = []
    opened_sessions = []
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_submitted = asyncio.Event()

    @asynccontextmanager
    async def sessionmaker():
        opened_sessions.append(object())
        yield SimpleNamespace(commit=AsyncMock())

    async def mark_status(session, dataset_id, *, status, **kwargs):
        states[dataset_id] = status
        transitions.append((dataset_id, status))

    async def get_dataset(session, dataset_id):
        return records[dataset_id]

    profile = SimpleNamespace(
        schema_=SimpleNamespace(row_count=2, col_count=1),
        health=SimpleNamespace(score=100, grade="A"),
    )

    def profile_sync(dataset_id, *args):
        if dataset_id == first_id:
            first_started.set()
            if not release_first.wait(timeout=5):
                raise RuntimeError("test barrier was not released")
            if first_fails:
                raise RuntimeError("mock profiling failure")
        else:
            second_started.set()
        return profile

    monkeypatch.setattr(pipeline, "_PIPELINE_SEMAPHORE", None)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_sessionmaker", lambda: sessionmaker)
    monkeypatch.setattr(pipeline.dataset_service, "get_dataset", get_dataset)
    monkeypatch.setattr(pipeline.dataset_service, "mark_status", mark_status)
    monkeypatch.setattr(pipeline, "_run_profile_sync", profile_sync)
    monkeypatch.setattr(pipeline, "_run_findings_sync", lambda *args: SimpleNamespace(count=0))
    monkeypatch.setattr(pipeline, "_run_viz_sync", lambda *args: SimpleNamespace(count=0))
    monkeypatch.setattr(pipeline, "_run_insights", AsyncMock(return_value=SimpleNamespace(
        explanation_count=0, question_count=0, degraded=True,
    )))
    monkeypatch.setattr(pipeline, "build_index", AsyncMock(return_value=SimpleNamespace(
        indexed_count=0, degraded=True,
    )))
    monkeypatch.setattr(pipeline, "logger", Mock())

    async def submit_second():
        second_submitted.set()
        await pipeline.run_profiling(second_id)

    tasks = [asyncio.create_task(pipeline.run_profiling(first_id))]
    try:
        assert await asyncio.to_thread(first_started.wait, 2)
        tasks.append(asyncio.create_task(submit_second()))
        await asyncio.wait_for(second_submitted.wait(), 2)
        # The second coroutine has reached the real semaphore and suspended.
        assert len(opened_sessions) == 1
        assert states[first_id] == DatasetStatus.PROFILING
        assert states[second_id] == DatasetStatus.QUEUED
        assert not second_started.is_set()
    finally:
        release_first.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)

    assert second_started.is_set()
    assert len(opened_sessions) == 2
    terminal = DatasetStatus.FAILED if first_fails else DatasetStatus.COMPLETE
    assert states == {first_id: terminal, second_id: DatasetStatus.COMPLETE}
    assert transitions.index((first_id, terminal)) < transitions.index(
        (second_id, DatasetStatus.PROFILING)
    )
