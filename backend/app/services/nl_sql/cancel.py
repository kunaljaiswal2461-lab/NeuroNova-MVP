"""Dropping a model call when the turn is cancelled (SPEC-09 W1.8)."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any

from app.connectors.postgres_query_executor import PostgresExecutionCancellation


class TurnCancelled(Exception):
    pass


async def cancellable(awaitable: Awaitable[Any], cancellation: PostgresExecutionCancellation | None) -> Any:
    """Await a model call as a task that is dropped when the turn is cancelled."""
    if cancellation is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(cancellation.wait_cancelled())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        raise TurnCancelled()
    finally:
        waiter.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
