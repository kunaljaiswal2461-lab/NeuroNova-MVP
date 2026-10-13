"""SPEC-05 B0: the SQLite DDL fallback in tests/conftest.py produces working server-side defaults."""
from __future__ import annotations

import uuid

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from app.db.models.user import User


async def test_server_generated_ids_and_timestamps_round_trip_on_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__]))
        # No id, created_at or updated_at supplied: the DEFAULTs must produce them.
        await connection.execute(insert(User.__table__).values(email="a@example.test", name="A"))
        await connection.execute(insert(User.__table__).values(email="b@example.test", name="B"))
        rows = (await connection.execute(select(User.__table__.c.id, User.__table__.c.created_at))).all()
    await engine.dispose()
    ids = [row.id for row in rows]
    assert all(isinstance(i, uuid.UUID) for i in ids)
    assert len(set(ids)) == 2 and all(i.version is None or i.int != 0 for i in ids)
    assert all(row.created_at is not None for row in rows)
