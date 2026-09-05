"""Real conditional SQL against isolated SQLite; no external DB access."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import MetaData, create_engine, insert, select, update

from app.db.models.dataset import DatasetRecord, DatasetStatus, FileType
from app.services import stale_dataset_reconciler as reconciler


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    # Copy schema without changing production metadata. UUIDs are provided by
    # tests, so omit PostgreSQL's uuid_generate_v4 default for SQLite DDL.
    metadata = MetaData()
    DatasetRecord.metadata.tables["users"].to_metadata(metadata)
    table = DatasetRecord.__table__.to_metadata(metadata)
    table.c.id.server_default = None
    table.create(engine)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def add_record(db, *, status=DatasetStatus.PROFILING, age=21):
    record_id = uuid.uuid4()
    db.execute(insert(DatasetRecord).values(
        id=record_id, filename="test.csv", original_name="test.csv",
        file_type=FileType.CSV, size_bytes=10, status=status, progress_pct=30,
        updated_at=NOW - timedelta(minutes=age),
        error_message="existing failure" if status == DatasetStatus.FAILED else None,
    ))
    db.commit()
    return record_id


def async_session(db):
    return SimpleNamespace(
        execute=AsyncMock(side_effect=db.execute), commit=AsyncMock(side_effect=db.commit),
    )


async def test_only_strictly_stale_profiling_rows_change(db):
    stale_id = add_record(db)
    fresh_id = add_record(db, age=19)
    boundary_id = add_record(db, age=20)
    others = {status: add_record(db, status=status, age=60)
              for status in DatasetStatus if status != DatasetStatus.PROFILING}
    session = async_session(db)
    assert await reconciler.reconcile_stale_datasets(session, now=NOW) == 1
    rows = {row.id: row for row in db.execute(select(DatasetRecord.__table__))}
    assert rows[stale_id].status == DatasetStatus.FAILED
    assert rows[stale_id].error_message == reconciler.INTERRUPTED_MESSAGE
    assert rows[stale_id].progress_pct == 30
    assert rows[fresh_id].status == DatasetStatus.PROFILING
    assert rows[boundary_id].status == DatasetStatus.PROFILING
    for status, record_id in others.items():
        assert rows[record_id].status == status
    assert rows[others[DatasetStatus.FAILED]].error_message == "existing failure"
    assert await reconciler.reconcile_stale_datasets(session, now=NOW) == 0


async def test_reconciliation_is_bounded(db, monkeypatch):
    monkeypatch.setattr(reconciler, "RECONCILE_BATCH_SIZE", 2)
    for _ in range(3):
        add_record(db)
    session = async_session(db)
    assert await reconciler.reconcile_stale_datasets(session, now=NOW) == 2
    assert await reconciler.reconcile_stale_datasets(session, now=NOW) == 1


@pytest.mark.parametrize("values", [
    {"status": DatasetStatus.COMPLETE},
    {"updated_at": NOW},
])
async def test_recent_change_before_update_is_not_clobbered(db, values):
    record_id = add_record(db)

    def execute_after_progress(statement):
        # Simulate a worker advancing immediately before the atomic statement.
        db.execute(update(DatasetRecord).where(DatasetRecord.id == record_id).values(**values))
        db.commit()
        # Both predicates must also be on the outer UPDATE, not only selection.
        outer_where = str(statement).split(") AND ")[-1]
        assert "dataset_records.status" in outer_where
        assert "dataset_records.updated_at" in outer_where
        return db.execute(statement)

    session = async_session(db)
    session.execute.side_effect = execute_after_progress
    assert await reconciler.reconcile_stale_datasets(session, now=NOW) == 0
    row = db.execute(select(DatasetRecord.__table__)).one()
    assert row.status == values.get("status", DatasetStatus.PROFILING)
    assert row.error_message is None


async def test_background_loop_recovers_from_exception_and_cancels(monkeypatch):
    attempted_twice = asyncio.Event()
    closed = Mock()

    @asynccontextmanager
    async def sessionmaker():
        try:
            yield object()
        finally:
            closed()

    async def reconcile(session):
        if operation.await_count == 1:
            raise RuntimeError("temporary database outage")
        attempted_twice.set()
        return 0

    operation = AsyncMock(side_effect=reconcile)
    logger = Mock()
    monkeypatch.setattr(reconciler, "get_sessionmaker", lambda: sessionmaker)
    monkeypatch.setattr(reconciler, "reconcile_stale_datasets", operation)
    monkeypatch.setattr(reconciler, "RECONCILE_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(reconciler, "logger", logger)
    task = asyncio.create_task(reconciler.run_stale_dataset_reconciler())
    try:
        await asyncio.wait_for(attempted_twice.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert operation.await_count >= 2
    assert closed.call_count >= 2
    logger.exception.assert_called_once_with("datasets.reconciliation_failed")


async def test_background_pass_timeout_closes_session(monkeypatch):
    closed = asyncio.Event()
    logger = Mock()

    @asynccontextmanager
    async def sessionmaker():
        try:
            yield object()
        finally:
            closed.set()

    async def blocked(session):
        await asyncio.Event().wait()

    monkeypatch.setattr(reconciler, "get_sessionmaker", lambda: sessionmaker)
    monkeypatch.setattr(reconciler, "reconcile_stale_datasets", blocked)
    monkeypatch.setattr(reconciler, "RECONCILE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(reconciler, "logger", logger)
    task = asyncio.create_task(reconciler.run_stale_dataset_reconciler())
    try:
        await asyncio.wait_for(closed.wait(), 1)
        logger.exception.assert_called_once()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("raise_inside", [False, True])
async def test_lifespan_cancels_reconciler_before_engine_disposal(monkeypatch, raise_inside):
    import main

    started = asyncio.Event()
    events = []

    async def background():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    async def dispose():
        events.append("disposed")

    monkeypatch.setattr(main, "run_stale_dataset_reconciler", background)
    monkeypatch.setattr(main, "dispose_engine", dispose)
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    try:
        async with main.lifespan(SimpleNamespace()):
            await asyncio.wait_for(started.wait(), 1)
            if raise_inside:
                raise RuntimeError("application failure")
    except RuntimeError:
        assert raise_inside
    assert events == ["cancelled", "disposed"]
