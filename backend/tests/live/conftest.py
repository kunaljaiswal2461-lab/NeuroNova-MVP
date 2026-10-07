"""Fixtures for the live_pg layer (SPEC-03 §10, §16.5). Skipped unless NN_LIVE_PG=1."""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from evals import pgrun


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fixture_context():
    from evals.bootstrap import bootstrap

    ctx = await bootstrap()
    yield ctx
    await ctx.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def grader():
    conn = await pgrun.connect_grader()
    yield conn
    await conn.close()


@pytest.fixture
def reader_password() -> str:
    return os.environ["NN_READER_PASSWORD"]


@pytest.fixture
def grader_password() -> str:
    return os.environ["NN_GRADER_PASSWORD"]
