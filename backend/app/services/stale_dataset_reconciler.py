"""Bounded DB-only recovery of interrupted profiling; no pipeline execution."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.dataset import DatasetRecord, DatasetStatus
from app.db.session import get_sessionmaker


logger = get_logger("stale_dataset_reconciler")
STALE_AFTER = timedelta(minutes=20)
RECONCILE_INTERVAL_SECONDS = 60
RECONCILE_TIMEOUT_SECONDS = 10
RECONCILE_BATCH_SIZE = 100
INTERRUPTED_MESSAGE = "Processing was interrupted. Please upload the dataset again."


async def reconcile_stale_datasets(
    session: AsyncSession, *, now: datetime | None = None,
) -> int:
    """Atomically fail at most one batch, rechecking status and age on update.

    Stage timestamps are not heartbeats: a live profiling stage that lasts
    over twenty minutes can also qualify. Do not infer liveness from this.
    """
    cutoff = (now or datetime.now(timezone.utc)) - STALE_AFTER
    stale = (
        DatasetRecord.status == DatasetStatus.PROFILING,
        DatasetRecord.updated_at < cutoff,
    )
    candidates = (
        select(DatasetRecord.id)
        .where(*stale)
        .order_by(DatasetRecord.updated_at, DatasetRecord.id)
        .limit(RECONCILE_BATCH_SIZE)
    )
    result = await session.execute(
        update(DatasetRecord)
        .where(DatasetRecord.id.in_(candidates), *stale)
        .values(
            status=DatasetStatus.FAILED,
            error_message=INTERRUPTED_MESSAGE,
            updated_at=now or datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount


async def run_stale_dataset_reconciler() -> None:
    """Run immediately at startup, then once a minute until lifespan shutdown."""
    while True:
        try:
            # Bound pool waits, queries and commits; close/rollback the session
            # on timeout or cancellation. Failures must not kill the loop.
            async with asyncio.timeout(RECONCILE_TIMEOUT_SECONDS):
                async with get_sessionmaker()() as session:
                    count = await reconcile_stale_datasets(session)
            if count:
                logger.info("datasets.interrupted", count=count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("datasets.reconciliation_failed")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
