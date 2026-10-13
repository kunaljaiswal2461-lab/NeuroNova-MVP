"""SPEC-09 L1-2: a rewritable server refusal becomes ``query_error`` plus its SQLSTATE, and nothing else."""
from __future__ import annotations

import logging

import asyncpg
import pytest

from app.connectors.postgres_query_executor import REPAIRABLE_SQLSTATES, PostgresExecutionQueryError
from tests.catalog.test_sql_execution import (  # noqa: F401  (fixtures + helpers)
    SQL,
    _connection,
    _run,
    execution_catalog,
    execution_session_factory,
    execution_settings,
)

SERVER_TEXT = 'column "zzz_secret" does not exist DETAIL: Key (email)=(someone@example.com) HINT: Perhaps you meant "id".'


class ServerRefusal(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(SERVER_TEXT)
        self.sqlstate = sqlstate
        self.message = SERVER_TEXT


@pytest.mark.parametrize("sqlstate", sorted(REPAIRABLE_SQLSTATES))
@pytest.mark.parametrize("stage", ["prepare", "explain", "cursor"])
async def test_each_repairable_sqlstate_maps_to_query_error(execution_catalog, sqlstate, stage, caplog):
    connection = _connection(execution_catalog, records=[(1, "1", "a")])
    exc = ServerRefusal(sqlstate)
    if stage == "prepare":
        async def prepare(sql):
            raise exc
        connection.prepare = prepare
    elif stage == "explain":
        async def fetchval(sql, *parameters):
            raise exc
        connection.fetchval = fetchval
    else:
        class Boom:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise exc

        connection.statement.cursor = lambda **kw: Boom()
    with caplog.at_level(logging.DEBUG):
        result, _ = await _run(execution_catalog, connection)
    assert result.status == "failed" and result.failure_code == "query_error" and result.error_class == sqlstate
    dumped = result.model_dump_json() + repr(result) + result.message
    for leaked in ("zzz_secret", "DETAIL", "someone@example.com", "HINT", "Perhaps"):
        assert leaked not in dumped and leaked not in caplog.text


@pytest.mark.parametrize("sqlstate", ["23505", "40001", "53200", "XX000", "42501", "57014", "08006"])
async def test_every_other_sqlstate_stays_opaque(execution_catalog, sqlstate):
    connection = _connection(execution_catalog, records=[])

    async def prepare(sql):
        raise ServerRefusal(sqlstate)

    connection.prepare = prepare
    result, _ = await _run(execution_catalog, connection)
    assert result.failure_code == "execution_failed" and result.error_class is None


async def test_an_exception_without_a_sqlstate_stays_opaque(execution_catalog):
    connection = _connection(execution_catalog, records=[])

    async def prepare(sql):
        raise RuntimeError(SERVER_TEXT)

    connection.prepare = prepare
    result, _ = await _run(execution_catalog, connection)
    assert result.failure_code == "execution_failed" and result.error_class is None
    assert "zzz_secret" not in result.model_dump_json()


async def test_a_real_asyncpg_grouping_error_is_a_query_error(execution_catalog):
    connection = _connection(execution_catalog, records=[])

    async def prepare(sql):
        raise asyncpg.exceptions.GroupingError(SERVER_TEXT)

    connection.prepare = prepare
    result, _ = await _run(execution_catalog, connection)
    assert (result.failure_code, result.error_class) == ("query_error", "42803")


async def test_the_row_size_guard_keeps_its_own_code(execution_catalog):
    from app.connectors import postgres_query_executor as mod

    connection = _connection(execution_catalog, records=[])
    err = asyncpg.InvalidTextRepresentationError(f'invalid input syntax for type integer: "{mod._ROW_TOO_LARGE_PREFIX}123"')

    async def prepare(sql):
        raise err

    connection.prepare = prepare
    result, _ = await _run(execution_catalog, connection)
    assert result.failure_code == "result_row_too_large" and result.error_class is None


async def test_a_plain_22p02_is_a_query_error(execution_catalog):
    connection = _connection(execution_catalog, records=[])

    async def prepare(sql):
        raise asyncpg.InvalidTextRepresentationError('invalid input syntax for type integer: "abc"')

    connection.prepare = prepare
    result, _ = await _run(execution_catalog, connection)
    assert (result.failure_code, result.error_class) == ("query_error", "22P02")


async def test_a_cancelled_query_is_not_reported_as_a_query_error(execution_catalog):
    from app.connectors.postgres_query_executor import PostgresExecutionCancellation

    cancellation = PostgresExecutionCancellation()
    connection = _connection(execution_catalog, records=[])

    async def prepare(sql):
        await cancellation.cancel()
        raise ServerRefusal("42601")

    connection.prepare = prepare
    result, _ = await _run(execution_catalog, connection, cancellation=cancellation)
    assert result.status == "cancelled" and result.error_class is None


async def test_the_compiled_path_keeps_mapping_undefined_columns_to_schema_changed():
    assert PostgresExecutionQueryError("42703").code == "query_error"
    from app.connectors.postgres_query_executor import PostgresQueryExecutionAdapter
    import inspect

    source = inspect.getsource(PostgresQueryExecutionAdapter._run_in_session)
    assert "UndefinedColumnError" in source and "PostgresExecutionSchemaChangedError" in source
