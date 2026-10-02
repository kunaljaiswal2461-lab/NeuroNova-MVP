"""JWT and response-boundary tests for PostgreSQL source HTTP APIs."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest

os.environ.setdefault("API_KEY", "stage2b-route-test-api-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.connectors.connection_policy import PostgresConnectionPolicy
from app.connectors.postgres_adapter import PostgresOnboardingAdapter
from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.source import SourceStatus, SourceType, TLSMode
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.schemas.source_schemas import (
    InventoryTableResponse,
    PostgresTestResponse,
    SourceDetailResponse,
    SourceListResponse,
)
from app.services import source_service
from main import create_app


class _AuthSession:
    def __init__(self, users):
        self.users = users

    async def get(self, model, identity):
        return self.users.get(identity) if model is User else None


def _source(owner_id: uuid.UUID) -> SourceDetailResponse:
    now = datetime.now(timezone.utc)
    return SourceDetailResponse(
        source_id=uuid.uuid4(),
        display_name="Warehouse",
        source_type=SourceType.POSTGRESQL,
        status=SourceStatus.TESTED,
        host="warehouse.example.com",
        port=5432,
        database_name="analytics",
        tls_mode=TLSMode.VERIFY_FULL,
        has_custom_ca=True,
        profiling_enabled=False,
        scope_count=1,
        last_tested_at=now,
        created_at=now,
        updated_at=now,
        selected_tables=[InventoryTableResponse(schema_name="public", table_name="orders")],
    )


@pytest.fixture
def route_context(monkeypatch):
    owner = User(id=uuid.uuid4(), email="owner@example.test", name="Owner", role=UserRole.USER, is_active=True)
    other = User(id=uuid.uuid4(), email="other@example.test", name="Other", role=UserRole.USER, is_active=True)
    settings = Settings(
        _env_file=None,
        api_key="stage2b-route-test-api-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="stage2b-route-test-jwt-secret",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id="test",
        connector_credentials_active_key="M2Secr7H2lWLMX6wAHxxJUT_lag81Dmkto5bDCoK1GU=",
    )
    source = _source(owner.id)
    calls: list[tuple[str, uuid.UUID | None]] = []

    async def fake_test(body, *, settings):
        calls.append(("test", None))
        return PostgresTestResponse(
            eligible_tables=[InventoryTableResponse(schema_name="public", table_name="orders")],
            count=1,
        )

    async def fake_create(_session, *, owner_user_id, **_kwargs):
        calls.append(("create", owner_user_id))
        return source

    async def fake_list(_session, *, owner_user_id):
        calls.append(("list", owner_user_id))
        return SourceListResponse(items=[] if owner_user_id != owner.id else [source], count=0 if owner_user_id != owner.id else 1)

    async def fake_read(_session, *, source_id, owner_user_id):
        calls.append(("read", owner_user_id))
        if source_id != source.source_id or owner_user_id != owner.id:
            raise NotFoundError("source not found")
        return source

    async def fake_delete(_session, *, source_id, owner_user_id):
        calls.append(("delete", owner_user_id))
        if source_id != source.source_id or owner_user_id != owner.id:
            raise NotFoundError("source not found")

    monkeypatch.setattr(source_service, "test_postgresql_source", fake_test)
    monkeypatch.setattr(source_service, "create_postgresql_source", fake_create)
    monkeypatch.setattr(source_service, "list_sources_for_owner", fake_list)
    monkeypatch.setattr(source_service, "read_source_for_owner", fake_read)
    monkeypatch.setattr(source_service, "delete_source_for_owner", fake_delete)

    app = create_app()

    async def override_session():
        yield _AuthSession({owner.id: owner, other.id: other})

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings

    def headers(user: User):
        return {
            "Authorization": "Bearer " + create_access_token(
                str(user.id), user.email, user.role.value, settings.jwt_secret_key, 30
            )
        }

    yield app, owner, other, source, settings, headers, calls
    app.dependency_overrides.clear()


@pytest.fixture
async def route_client(route_context):
    app, *_ = route_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _test_payload():
    return {
        "host": "warehouse.example.com",
        "database_name": "analytics",
        "credentials": {"username": "reporter", "password": "sensitive-password"},
    }


def _create_payload():
    return {
        **_test_payload(),
        "display_name": "Warehouse",
        "selected_tables": [{"schema_name": "public", "table_name": "orders"}],
    }


async def test_jwt_owner_can_test_create_read_list_and_delete(route_client, route_context):
    _app, owner, _other, source, _settings, headers, calls = route_context
    owner_headers = headers(owner)

    assert (await route_client.post("/api/v1/sources/postgresql/test", headers=owner_headers, json=_test_payload())).status_code == 200
    created = await route_client.post("/api/v1/sources", headers=owner_headers, json=_create_payload())
    assert created.status_code == 201
    assert "sensitive-password" not in created.text
    assert "ciphertext" not in created.text
    assert (await route_client.get("/api/v1/sources", headers=owner_headers)).status_code == 200
    assert (await route_client.get(f"/api/v1/sources/{source.source_id}", headers=owner_headers)).status_code == 200
    assert (await route_client.delete(f"/api/v1/sources/{source.source_id}", headers=owner_headers)).status_code == 204
    assert [name for name, _owner_id in calls] == ["test", "create", "list", "read", "delete"]


async def test_other_user_gets_non_enumerating_not_found(route_client, route_context):
    _app, _owner, other, source, _settings, headers, calls = route_context
    response = await route_client.get(f"/api/v1/sources/{source.source_id}", headers=headers(other))
    assert response.status_code == 404
    deleted = await route_client.delete(f"/api/v1/sources/{source.source_id}", headers=headers(other))
    assert deleted.status_code == 404
    assert [name for name, _owner_id in calls] == ["read", "delete"]


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "stage2b-route-test-api-key"}, {"Authorization": "Bearer invalid"}])
async def test_source_apis_reject_unauthenticated_api_key_and_invalid_bearer(route_client, headers):
    response = await route_client.post("/api/v1/sources/postgresql/test", headers=headers, json=_test_payload())
    assert response.status_code == 401


async def test_source_validation_never_reflects_password_or_ca(route_client, route_context):
    _app, owner, _other, _source, _settings, headers, calls = route_context
    payload = _test_payload()
    payload["credentials"]["password"] = "do-not-reflect-this-password"
    payload["tls"] = {"mode": "verify-full", "custom_ca_pem": "x" * (64 * 1024 + 1)}
    response = await route_client.post("/api/v1/sources/postgresql/test", headers=headers(owner), json=payload)
    assert response.status_code == 422
    assert "do-not-reflect-this-password" not in response.text
    assert "x" * 64 not in response.text
    assert calls == []


@pytest.mark.parametrize("custom_ca_pem", ["", " \n\t "])
async def test_blank_custom_ca_is_rejected_without_contacting_the_adapter(
    route_client,
    route_context,
    custom_ca_pem,
):
    _app, owner, _other, _source, _settings, headers, calls = route_context
    payload = _test_payload()
    payload["tls"] = {"mode": "verify-full", "custom_ca_pem": custom_ca_pem}

    response = await route_client.post(
        "/api/v1/sources/postgresql/test", headers=headers(owner), json=payload
    )

    assert response.status_code == 422
    assert calls == []


class _FixtureTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FixtureConnection:
    def __init__(self):
        self.closed = False

    def transaction(self, *, readonly):
        assert readonly is True
        return _FixtureTransaction()

    async def fetch(self, *_args):
        return [{"table_schema": "fixture", "table_name": "allowed"}]

    async def close(self):
        self.closed = True


async def test_http_fixture_can_inject_the_explicit_disposable_destination_policy(monkeypatch):
    owner = User(
        id=uuid.uuid4(),
        email="fixture-owner@example.test",
        name="Fixture owner",
        role=UserRole.USER,
        is_active=True,
    )
    settings = Settings(
        _env_file=None,
        api_key="stage2b-route-test-api-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="stage2b-route-test-jwt-secret",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id="test",
        connector_credentials_active_key="M2Secr7H2lWLMX6wAHxxJUT_lag81Dmkto5bDCoK1GU=",
    )

    async def private_fixture_resolver(host: str, port: int) -> list[str]:
        assert host == "client-postgres.stage2d.test"
        assert port == 5432
        return ["127.0.0.1"]

    connection = _FixtureConnection()

    async def connect(**_kwargs):
        return connection

    adapter = PostgresOnboardingAdapter(
        settings=settings,
        policy=PostgresConnectionPolicy.for_disposable_test(private_fixture_resolver),
        connect=connect,
    )
    monkeypatch.setattr(source_service, "_build_adapter", lambda _settings: adapter)

    app = create_app()

    async def override_session():
        yield _AuthSession({owner.id: owner})

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    headers = {
        "Authorization": "Bearer " + create_access_token(
            str(owner.id), owner.email, owner.role.value, settings.jwt_secret_key, 30
        )
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/sources/postgresql/test",
                headers=headers,
                json={
                    "host": "client-postgres.stage2d.test",
                    "database_name": "client_fixture",
                    "credentials": {"username": "reader", "password": "synthetic-password"},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["eligible_tables"] == [{"schema_name": "fixture", "table_name": "allowed"}]
    assert connection.closed is True


async def test_profiling_cannot_be_enabled_in_stage_2b(route_client, route_context):
    _app, owner, _other, _source, _settings, headers, calls = route_context
    payload = _create_payload()
    payload["profiling_enabled"] = True
    response = await route_client.post("/api/v1/sources", headers=headers(owner), json=payload)
    assert response.status_code == 422
    assert calls == []


async def test_source_routes_use_configured_cors_origins_not_wildcard(route_client):
    allowed = await route_client.options(
        "/api/v1/sources",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

    rejected = await route_client.options(
        "/api/v1/sources",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in rejected.headers
