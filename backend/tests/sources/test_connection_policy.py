from __future__ import annotations

import asyncio

import pytest

from app.connectors.connection_policy import ConnectionPolicyError, PostgresConnectionPolicy
from app.connectors.postgres_adapter import (
    InventoryOverflowError,
    PostgresOnboardingAdapter,
    PostgresTimeoutError,
)
from app.core.config import Settings


async def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["1.1.1.1", "2606:4700:4700::1111"]


async def _private_resolver(_host: str, _port: int) -> list[str]:
    return ["127.0.0.1", "fd00::1"]


def _settings(**overrides) -> Settings:
    values = {
        "api_key": "stage2b-test-api-key",
        "database_url": "sqlite+aiosqlite://",
        "connector_max_inventory_tables": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_production_policy_accepts_public_dns_and_rejects_private_or_url_inputs():
    policy = PostgresConnectionPolicy(resolver=_public_resolver)
    assert await policy.validate(host="database.example.com", port=5432) == "database.example.com"
    with pytest.raises(ConnectionPolicyError):
        await policy.validate(host="postgresql://database.example.com/db", port=5432)
    with pytest.raises(ConnectionPolicyError):
        await policy.validate(host="127.0.0.1", port=5432)
    with pytest.raises(ConnectionPolicyError):
        await policy.validate(host="database.example.com", port=15432)
    with pytest.raises(ConnectionPolicyError):
        await PostgresConnectionPolicy(resolver=_private_resolver).validate(
            host="database.example.com", port=5432
        )


def test_test_only_policy_must_be_explicit_and_tls_is_verify_full():
    policy = PostgresConnectionPolicy.for_disposable_test(_private_resolver)
    assert policy.allow_non_public_for_disposable_tests is True
    assert policy.allow_single_label_for_disposable_tests is True
    context = policy.build_ssl_context(None)
    assert context.check_hostname is True
    assert context.verify_mode.name == "CERT_REQUIRED"


@pytest.mark.asyncio
async def test_test_policy_can_use_disposable_localhost_without_weakening_production():
    policy = PostgresConnectionPolicy.for_disposable_test(_private_resolver)
    assert await policy.validate(host="localhost", port=5432) == "localhost"
    with pytest.raises(ConnectionPolicyError):
        await PostgresConnectionPolicy(resolver=_private_resolver).validate(
            host="localhost", port=5432
        )


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.readonly = True

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.readonly = False
        self.fetch_args = None

    def transaction(self, *, readonly):
        assert readonly is True
        return _Transaction(self)

    async def fetch(self, *args):
        self.fetch_args = args
        return self.rows

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_adapter_closes_after_success_and_detects_inventory_overflow():
    connection = _Connection([{"table_schema": "public", "table_name": "orders"}])

    async def connect(**_kwargs):
        return connection

    adapter = PostgresOnboardingAdapter(
        settings=_settings(),
        policy=PostgresConnectionPolicy(resolver=_public_resolver),
        connect=connect,
    )
    tables = await adapter.test_and_list_tables(
        host="database.example.com", port=5432, database_name="app", username="u", password="p", custom_ca_pem=None
    )
    assert [(item.schema_name, item.table_name) for item in tables] == [("public", "orders")]
    assert connection.readonly is True
    assert connection.closed is True
    assert connection.fetch_args[1] == 3

    overflow_connection = _Connection([
        {"table_schema": "public", "table_name": name} for name in ("a", "b", "c")
    ])

    async def overflow_connect(**_kwargs):
        return overflow_connection

    overflow_adapter = PostgresOnboardingAdapter(
        settings=_settings(), policy=PostgresConnectionPolicy(resolver=_public_resolver), connect=overflow_connect
    )
    with pytest.raises(InventoryOverflowError):
        await overflow_adapter.test_and_list_tables(
            host="database.example.com", port=5432, database_name="app", username="u", password="p", custom_ca_pem=None
        )
    assert overflow_connection.closed is True


@pytest.mark.asyncio
async def test_adapter_maps_timeout_and_closes_after_cancelled_fetch():
    async def timeout_connect(**_kwargs):
        raise TimeoutError()

    timeout_adapter = PostgresOnboardingAdapter(
        settings=_settings(), policy=PostgresConnectionPolicy(resolver=_public_resolver), connect=timeout_connect
    )
    with pytest.raises(PostgresTimeoutError):
        await timeout_adapter.test_and_list_tables(
            host="database.example.com", port=5432, database_name="app", username="u", password="p", custom_ca_pem=None
        )

    class _SlowConnection(_Connection):
        async def fetch(self, *args):
            self.fetch_args = args
            await asyncio.Event().wait()

    connection = _SlowConnection([])

    async def slow_connect(**_kwargs):
        return connection

    adapter = PostgresOnboardingAdapter(
        settings=_settings(connector_operation_timeout_seconds=30),
        policy=PostgresConnectionPolicy(resolver=_public_resolver),
        connect=slow_connect,
    )
    task = asyncio.create_task(adapter.test_and_list_tables(
        host="database.example.com", port=5432, database_name="app", username="u", password="p", custom_ca_pem=None
    ))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.closed is True


@pytest.mark.asyncio
async def test_adapter_operation_timeout_bounds_dns_validation_before_connecting():
    async def slow_resolver(_host: str, _port: int) -> list[str]:
        await asyncio.Event().wait()
        return []

    connected = False

    async def connect(**_kwargs):
        nonlocal connected
        connected = True
        return _Connection([])

    adapter = PostgresOnboardingAdapter(
        settings=_settings(connector_operation_timeout_seconds=0.01),
        policy=PostgresConnectionPolicy(resolver=slow_resolver),
        connect=connect,
    )

    with pytest.raises(PostgresTimeoutError):
        await adapter.test_and_list_tables(
            host="database.example.com",
            port=5432,
            database_name="app",
            username="u",
            password="p",
            custom_ca_pem=None,
        )
    assert connected is False
