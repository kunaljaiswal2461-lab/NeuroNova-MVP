"""Build, inside the fixture, what a real owner would have (SPEC-03 §10).

A verified user, a saved PostgreSQL source created through the normal
``create_postgresql_source`` path, and a published catalog snapshot produced by
``CatalogDiscoveryWorker``. The *only* substitution is the documented test
connection policy (``PostgresConnectionPolicy.for_disposable_test``), injected
through the adapter factory. No production module is modified (INV-P0-10).
"""
from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.connectors.connection_policy import PostgresConnectionPolicy
from app.connectors.postgres_adapter import PostgresOnboardingAdapter
from app.connectors.postgres_query_executor import PostgresQueryExecutionAdapter
from app.connectors.postgres_sample_adapter import PostgresSampleAdapter
from app.core.config import Settings
from app.db.models.catalog import CatalogSnapshot, CatalogSyncRun, CatalogSyncRunStatus
from app.db.models.user import User, UserRole
from app.schemas.source_schemas import CreateSourceRequest
from app.services import catalog_service, source_service
from app.workers.catalog_discovery_worker import CatalogDiscoveryWorker
from evals import guards, pgrun

IN_SCOPE_TABLES: tuple[str, ...] = (
    "regions", "customers", "products", "orders", "order_items", "refunds", "Order Events", "page_views",
)


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return sorted({info[4][0] for info in infos})


def test_policy() -> PostgresConnectionPolicy:
    """Docker addresses are private, which the production policy correctly rejects."""
    return PostgresConnectionPolicy.for_disposable_test(resolver=_system_resolver)


@dataclass(frozen=True)
class FixtureContext:
    user_id: uuid.UUID
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    query_adapter_factory: Callable[[], PostgresQueryExecutionAdapter]

    async def dispose(self) -> None:
        await self.engine.dispose()


def settings_from_env() -> Settings:
    return Settings(_env_file=None)       # the process environment only; never a developer's .env


async def bootstrap(
    env: Mapping[str, str] | None = None,
    *,
    selected_tables: Sequence[tuple[str, str]] | None = None,
) -> FixtureContext:
    """``selected_tables`` (schema, table) defaults to the shop fixture's eight in-scope tables."""
    env = os.environ if env is None else env
    guards.assert_fixture_environment(env)              # INV-P0-01, before anything else
    settings = settings_from_env()
    guards.assert_fixture_hosts(settings.database_url, guards.FIXTURE_CLIENT_HOST)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    policy = test_policy()

    def adapter_factory(adapter_settings: Settings) -> PostgresOnboardingAdapter:
        return PostgresOnboardingAdapter(settings=adapter_settings, policy=policy)

    suffix = uuid.uuid4().hex[:12]
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"eval-{suffix}@example.test",
            name="Eval Owner",
            role=UserRole.USER,
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()

    request = CreateSourceRequest(
        display_name=f"Eval fixture {suffix}",
        host=guards.FIXTURE_CLIENT_HOST,
        port=5432,
        database_name=pgrun.DATABASE,
        tls={"mode": "verify-full", "custom_ca_pem": pgrun.ca_pem()},
        credentials={"username": "nn_reader", "password": env["NN_READER_PASSWORD"]},
        selected_tables=[
            {"schema_name": schema, "table_name": table}
            for schema, table in (selected_tables or [("shop", t) for t in IN_SCOPE_TABLES])
        ],
    )
    async with session_factory() as session:
        created = await source_service.create_postgresql_source(
            session,
            owner_user_id=user.id,
            request=request,
            settings=settings,
            adapter_factory=adapter_factory,
        )
    source_id = created.source_id

    async with session_factory() as session:
        run = await catalog_service.request_catalog_sync_for_owner(
            session, source_id=source_id, owner_user_id=user.id
        )
        run_id = run.id
    worker = CatalogDiscoveryWorker(
        settings=settings,
        session_factory=session_factory,
        adapter_factory=adapter_factory,
        # SPEC-07: sample values are collected during the sync; the disposable-test policy is the only substitution.
        sample_adapter_factory=lambda sample_settings: PostgresSampleAdapter(settings=sample_settings, policy=policy),
        worker_id=f"eval-bootstrap-{suffix}",
    )
    status = None
    for _ in range(120):
        await worker.run_once()
        async with session_factory() as session:
            status = (await session.get(CatalogSyncRun, run_id)).status
        if status in (CatalogSyncRunStatus.SUCCEEDED, CatalogSyncRunStatus.FAILED, CatalogSyncRunStatus.CANCELLED):
            break
        await asyncio.sleep(0.25)
    if status is not CatalogSyncRunStatus.SUCCEEDED:
        raise RuntimeError(f"catalog sync did not succeed: {status}")

    async with session_factory() as session:
        snapshot = await catalog_service.get_current_catalog_snapshot_for_owner(
            session, source_id=source_id, owner_user_id=user.id
        )
        assert isinstance(snapshot, CatalogSnapshot)
        snapshot_id = snapshot.id

    return FixtureContext(
        user_id=user.id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        query_adapter_factory=lambda: PostgresQueryExecutionAdapter(settings=settings, policy=policy),
    )
