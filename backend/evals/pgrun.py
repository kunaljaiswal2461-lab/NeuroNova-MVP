"""asyncpg helpers for the fixture's client database (SPEC-03 §8, §13).

Every connection goes through ``connect_as`` so INV-P0-01 holds here too: the
only hosts it will ever dial are the fixture's own network aliases.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import asyncpg

from app.connectors.connection_policy import PostgresConnectionPolicy
from evals import guards
from evals.grader import ResultColumn, ResultSet

DATABASE = "nn_client"
_ALLOWED_HOSTS = {guards.FIXTURE_CLIENT_HOST, guards.FIXTURE_CLIENT_ALT_HOST}


def ca_pem() -> str:
    return Path(os.environ.get("NN_CA_PEM_PATH", "/certs/ca.crt")).read_text()


async def connect_as(
    role: str,
    password: str,
    *,
    host: str = guards.FIXTURE_CLIENT_HOST,
    custom_ca_pem: str | None = None,
    ssl: Any = "verify-full",
) -> asyncpg.Connection:
    """Open a TLS-verified connection to the fixture client DB as ``role``."""
    if host not in _ALLOWED_HOSTS:
        raise guards.FixtureGuardError(f"refusing to connect to {host!r}: not a fixture host")
    if ssl == "verify-full":
        ssl = PostgresConnectionPolicy.build_ssl_context(custom_ca_pem if custom_ca_pem is not None else ca_pem())
    return await asyncpg.connect(
        host=host,
        port=5432,
        user=role,
        password=password,
        database=DATABASE,
        ssl=ssl,
        timeout=10,
        server_settings={"TimeZone": "UTC"},
    )


async def connect_grader() -> asyncpg.Connection:
    guards.assert_fixture_environment()
    return await connect_as("nn_grader", os.environ["NN_GRADER_PASSWORD"])


async def connect_drift() -> asyncpg.Connection:
    """nn_drift: owns the scratch `drift` schema only (SPEC-05 drift tests)."""
    guards.assert_fixture_environment()
    return await connect_as("nn_drift", os.environ["NN_DRIFT_PASSWORD"])


async def run_raw(conn: asyncpg.Connection, sql: str) -> tuple[list[str], list[tuple]]:
    stmt = await conn.prepare(sql)
    rows = await stmt.fetch()
    return [a.name for a in stmt.get_attributes()], [tuple(r) for r in rows]


async def run_result(conn: asyncpg.Connection, sql: str, *, max_rows: int = 1000) -> ResultSet:
    """Run ``sql`` and return a typed :class:`ResultSet`; ``complete`` is False if capped."""
    stmt = await conn.prepare(sql)
    columns = tuple(ResultColumn(a.name, a.type.name) for a in stmt.get_attributes())
    async with conn.transaction(readonly=True):
        cursor = await stmt.cursor()
        rows = await cursor.fetch(max_rows + 1)
    complete = len(rows) <= max_rows
    return ResultSet(columns=columns, rows=tuple(tuple(r) for r in rows[:max_rows]), complete=complete)
