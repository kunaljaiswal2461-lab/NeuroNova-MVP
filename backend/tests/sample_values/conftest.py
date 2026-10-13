"""Shared L1 fixtures for SPEC-07: a SQLite catalog published through the real worker with a fake sample reader."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.postgres_adapter import DiscoveredColumn, DiscoveredTable, DiscoveredKey
from app.connectors.postgres_query_executor import PostgresExecutionAccessChangedError
from app.connectors.postgres_sample_adapter import RawColumnStats, SampleTableResult
from app.db.base import Base
from app.db.models.user import User, UserRole
from app.services.catalog_service import request_catalog_sync_for_owner
from app.workers.catalog_discovery_worker import CatalogDiscoveryWorker
from tests.catalog.test_catalog_discovery_worker import _TABLES, _make_source, _settings

TABLES = list(_TABLES)


def text_col(name: str, pos: int, kind: str = "text") -> DiscoveredColumn:
    return DiscoveredColumn(name, pos, True, kind, "pg_catalog", kind, 25, -1, None)


def int_col(name: str, pos: int) -> DiscoveredColumn:
    return DiscoveredColumn(name, pos, False, "integer", "pg_catalog", "int4", 23, -1, None)


def shop_metadata() -> tuple[DiscoveredTable, ...]:
    return (
        DiscoveredTable(
            "sales", "customers", None,
            (int_col("customer_id", 1), text_col("segment", 2), text_col("full_name", 3), text_col("email", 4),
             text_col("city", 5)),
            (DiscoveredKey("customers_pkey", "p", ("customer_id",)),), (),
        ),
        DiscoveredTable(
            "sales", "orders", None,
            (int_col("order_id", 1), text_col("status", 2, "varchar"), text_col("note", 3)),
            (DiscoveredKey("orders_pkey", "p", ("order_id",)),), (),
        ),
    )


def stats(vals, freqs, n=None, null=0.0) -> RawColumnStats:
    return RawColumnStats(float(n if n is not None else len(vals)), null, tuple(vals), tuple(freqs))


def default_results() -> dict[tuple[str, str], SampleTableResult]:
    return {
        ("sales", "customers"): SampleTableResult(
            reltuples=5000.0, relation_bytes=1000,
            stats={
                "segment": stats(["Retail", "SMB", "Enterprise", "enterprise "], [0.4, 0.3, 0.2976, 0.0024]),
                "city": RawColumnStats(62.0, 0.0, ("A",), (0.02,)),
            },
        ),
        ("sales", "orders"): SampleTableResult(
            reltuples=100.0, relation_bytes=1000,
            stats={
                "status": stats(["PAID", "pending", "Cancelled", "REFUNDED"], [0.5, 0.2, 0.2, 0.1]),
                "note": stats(["someone@example.com", "ok"], [0.5, 0.5]),
            },
        ),
    }


class FakeReader:
    """Stands in for ``PostgresSampleAdapter``: returns canned results, records calls, can fail per table."""

    def __init__(self, results=None, fail=()):
        self.results = default_results() if results is None else results
        self.fail = set(fail)
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.hook = None

    async def read_table(self, *, request, **_connection):
        key = (request.schema_name, request.table_name)
        self.calls.append((*key, tuple(c.name for c in request.columns)))
        if self.hook is not None:
            await self.hook(key)
        if key in self.fail:
            raise PostgresExecutionAccessChangedError()
        return self.results[key]


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _Discovery:
    def __init__(self, tables):
        self.tables = tables

    async def discover_selected_tables(self, **_kwargs):
        return self.tables


@pytest.fixture
def settings():
    return _settings()


@pytest.fixture
async def world(factory, settings):
    """A source (owned by ``owner``) with no snapshot yet, plus a ``sync(reader)`` helper."""
    owner_id, source_id = await _make_source(factory, settings)
    async with factory() as session:
        other = User(id=uuid.uuid4(), email="other@example.test", name="Other", role=UserRole.USER,
                     is_active=True, is_verified=True)
        session.add(other)
        await session.commit()

    async def sync(reader: FakeReader | None, tables=None):
        async with factory() as session:
            await request_catalog_sync_for_owner(session, source_id=source_id, owner_user_id=owner_id)
        worker = CatalogDiscoveryWorker(
            settings=settings, session_factory=factory,
            adapter_factory=lambda _s: _Discovery(tables or shop_metadata()),
            sample_adapter_factory=lambda _s: reader, worker_id="w",
        )
        assert await worker.run_once()

    return SimpleNamespace(factory=factory, settings=settings, owner_id=owner_id, source_id=source_id,
                           other_id=other.id, sync=sync)
