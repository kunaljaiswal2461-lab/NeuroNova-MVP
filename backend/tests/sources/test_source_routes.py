"""JWT and response-boundary tests for PostgreSQL source HTTP APIs."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

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
from app.db.models.catalog import (
    CatalogForeignKeyTargetStatus,
    CatalogKeyKind,
    CatalogSyncRunStatus,
)
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.schemas.source_schemas import (
    InventoryTableResponse,
    PostgresTestRequest,
    PostgresTestResponse,
    SourceDetailResponse,
    SourceListResponse,
)
from app.services import catalog_service, source_service
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
    catalog_run = SimpleNamespace(
        id=uuid.uuid4(),
        status=CatalogSyncRunStatus.QUEUED,
        requested_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=None,
        attempt_count=0,
        next_attempt_at=None,
        failure_code=None,
    )
    catalog_snapshot = SimpleNamespace(
        id=uuid.uuid4(),
        published_at=datetime.now(timezone.utc),
    )
    previous_catalog_snapshot = SimpleNamespace(
        id=uuid.uuid4(),
        published_at=datetime.now(timezone.utc),
    )
    catalog_asset = catalog_service.CatalogAssetSummary(
        asset_id=uuid.uuid4(),
        schema_name="public",
        table_name="orders",
        table_comment="<img src=x onerror=alert(1)>",
    )
    customer_asset = catalog_service.CatalogAssetReference(
        asset_id=uuid.uuid4(),
        schema_name="public",
        table_name="customers",
    )
    catalog_page = catalog_service.CatalogAssetPage(
        source_id=source.source_id,
        snapshot=catalog_snapshot,
        scope_matches_current_selection=True,
        page=1,
        page_size=50,
        total=1,
        assets=(catalog_asset,),
    )
    catalog_detail = catalog_service.CatalogAssetDetail(
        source_id=source.source_id,
        snapshot=catalog_snapshot,
        scope_matches_current_selection=True,
        asset=catalog_asset,
        columns=(
            catalog_service.CatalogColumnMetadata(
                column_name="tenant_id",
                ordinal_position=1,
                is_nullable=False,
                formatted_native_type="integer",
                native_type_schema="pg_catalog",
                native_type_name="int4",
                native_type_oid=23,
                native_type_modifier=-1,
                column_comment="database column comment",
            ),
            catalog_service.CatalogColumnMetadata(
                column_name="customer_id",
                ordinal_position=2,
                is_nullable=False,
                formatted_native_type="integer",
                native_type_schema="pg_catalog",
                native_type_name="int4",
                native_type_oid=23,
                native_type_modifier=-1,
                column_comment=None,
            ),
        ),
        declared_keys=(
            catalog_service.CatalogDeclaredKeyMetadata(
                constraint_name="orders_customer_key",
                key_kind=CatalogKeyKind.UNIQUE,
                column_names=("tenant_id", "customer_id"),
            ),
        ),
        foreign_keys=(
            catalog_service.CatalogForeignKeyMetadata(
                constraint_name="orders_customer_fkey",
                target_status=CatalogForeignKeyTargetStatus.IN_SCOPE,
                referenced_asset=customer_asset,
                column_mappings=(
                    catalog_service.CatalogForeignKeyColumnMapping(
                        ordinal_position=1,
                        referencing_column_name="tenant_id",
                        referenced_column_name="tenant_id",
                    ),
                    catalog_service.CatalogForeignKeyColumnMapping(
                        ordinal_position=2,
                        referencing_column_name="customer_id",
                        referenced_column_name="customer_id",
                    ),
                ),
            ),
        ),
    )
    catalog_diff = catalog_service.CatalogStructuralDiff(
        source_id=source.source_id,
        snapshot=catalog_snapshot,
        state="compared",
        previous_snapshot=previous_catalog_snapshot,
        truncated=False,
        change_limit=500,
        columns_changed=(
            catalog_service.CatalogColumnChange(
                schema_name="public",
                table_name="orders",
                column_name="tenant_id",
                previous_formatted_native_type="integer",
                current_formatted_native_type="bigint",
                previous_is_nullable=False,
                current_is_nullable=False,
            ),
        ),
    )

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

    async def fake_catalog_request(_session, *, source_id, owner_user_id):
        calls.append(("catalog_request", owner_user_id))
        if source_id != source.source_id or owner_user_id != owner.id:
            raise NotFoundError("source not found")
        return catalog_run

    async def fake_catalog_availability(_session, *, source_id, owner_user_id):
        calls.append(("catalog_availability", owner_user_id))
        if source_id != source.source_id or owner_user_id != owner.id:
            raise NotFoundError("source not found")
        return catalog_service.CatalogAvailability(
            source_id=source_id,
            latest_run=catalog_run,
            current_snapshot=None,
        )

    async def fake_catalog_run(_session, *, source_id, run_id, owner_user_id):
        calls.append(("catalog_run", owner_user_id))
        if source_id != source.source_id or run_id != catalog_run.id or owner_user_id != owner.id:
            raise NotFoundError("catalog synchronization run not found")
        return catalog_run

    async def fake_catalog_cancel(_session, *, source_id, run_id, owner_user_id):
        calls.append(("catalog_cancel", owner_user_id))
        if source_id != source.source_id or run_id != catalog_run.id or owner_user_id != owner.id:
            raise NotFoundError("catalog synchronization run not found")
        catalog_run.status = CatalogSyncRunStatus.CANCELLED
        catalog_run.completed_at = datetime.now(timezone.utc)
        return catalog_run

    async def fake_catalog_assets(
        _session,
        *,
        source_id,
        snapshot_id,
        owner_user_id,
        page,
        page_size,
        search,
    ):
        calls.append(("catalog_assets", owner_user_id))
        if (
            source_id != source.source_id
            or snapshot_id != catalog_snapshot.id
            or owner_user_id != owner.id
        ):
            raise NotFoundError("catalog snapshot not found")
        assert page == 1
        assert page_size == 50
        assert search is None
        return catalog_page

    async def fake_catalog_asset_detail(
        _session,
        *,
        source_id,
        snapshot_id,
        asset_id,
        owner_user_id,
    ):
        calls.append(("catalog_asset_detail", owner_user_id))
        if (
            source_id != source.source_id
            or snapshot_id != catalog_snapshot.id
            or asset_id != catalog_asset.asset_id
            or owner_user_id != owner.id
        ):
            raise NotFoundError("catalog asset not found")
        return catalog_detail

    async def fake_catalog_diff(_session, *, source_id, snapshot_id, owner_user_id):
        calls.append(("catalog_diff", owner_user_id))
        if (
            source_id != source.source_id
            or snapshot_id != catalog_snapshot.id
            or owner_user_id != owner.id
        ):
            raise NotFoundError("catalog snapshot not found")
        return catalog_diff

    monkeypatch.setattr(source_service, "test_postgresql_source", fake_test)
    monkeypatch.setattr(source_service, "create_postgresql_source", fake_create)
    monkeypatch.setattr(source_service, "list_sources_for_owner", fake_list)
    monkeypatch.setattr(source_service, "read_source_for_owner", fake_read)
    monkeypatch.setattr(source_service, "delete_source_for_owner", fake_delete)
    monkeypatch.setattr(catalog_service, "request_catalog_sync_for_owner", fake_catalog_request)
    monkeypatch.setattr(catalog_service, "get_catalog_availability_for_owner", fake_catalog_availability)
    monkeypatch.setattr(catalog_service, "get_catalog_sync_run_for_owner", fake_catalog_run)
    monkeypatch.setattr(catalog_service, "cancel_catalog_sync_run_for_owner", fake_catalog_cancel)
    monkeypatch.setattr(catalog_service, "list_catalog_assets_for_owner", fake_catalog_assets)
    monkeypatch.setattr(catalog_service, "get_catalog_asset_detail_for_owner", fake_catalog_asset_detail)
    monkeypatch.setattr(catalog_service, "get_catalog_structural_diff_for_owner", fake_catalog_diff)

    catalog_run.catalog_snapshot = catalog_snapshot
    catalog_run.catalog_asset = catalog_asset

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

    yield app, owner, other, source, settings, headers, calls, catalog_run
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


def test_postgres_test_route_keeps_body_and_settings_dependency_bound():
    """Guard the SlowAPI/FastAPI binding contract used by the deployed image."""
    app = create_app()
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/sources/postgresql/test")

    # SlowAPI's wrapper in the pinned FastAPI runtime cannot resolve postponed
    # annotations from this route module.  Keep this check independent of the
    # local FastAPI version so re-adding the future import fails CI.
    assert route.endpoint.__annotations__["body"] is PostgresTestRequest
    assert [parameter.name for parameter in route.dependant.body_params] == ["body"]
    assert [parameter.name for parameter in route.dependant.query_params] == []
    assert {dependency.name for dependency in route.dependant.dependencies} >= {"settings", "current_user"}


async def test_jwt_owner_can_test_create_read_list_and_delete(route_client, route_context):
    _app, owner, _other, source, _settings, headers, calls, _catalog_run = route_context
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
    _app, _owner, other, source, _settings, headers, calls, _catalog_run = route_context
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
    _app, owner, _other, _source, _settings, headers, calls, _catalog_run = route_context
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
    _app, owner, _other, _source, _settings, headers, calls, _catalog_run = route_context
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
    _app, owner, _other, _source, _settings, headers, calls, _catalog_run = route_context
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


async def test_catalog_sync_routes_are_jwt_owner_scoped(route_client, route_context):
    _app, owner, _other, source, _settings, headers, calls, catalog_run = route_context
    owner_headers = headers(owner)

    accepted = await route_client.post(
        f"/api/v1/sources/{source.source_id}/catalog-syncs", headers=owner_headers
    )
    assert accepted.status_code == 202
    assert accepted.json()["run_id"] == str(catalog_run.id)
    availability = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog", headers=owner_headers
    )
    assert availability.status_code == 200
    assert availability.json()["current_snapshot_id"] is None
    inspected = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog-syncs/{catalog_run.id}",
        headers=owner_headers,
    )
    assert inspected.status_code == 200
    cancelled = await route_client.post(
        f"/api/v1/sources/{source.source_id}/catalog-syncs/{catalog_run.id}/cancel",
        headers=owner_headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    assert [name for name, _owner_id in calls] == [
        "catalog_request",
        "catalog_availability",
        "catalog_run",
        "catalog_cancel",
    ]


async def test_catalog_sync_routes_reject_other_user_and_legacy_api_key(route_client, route_context):
    _app, _owner, other, source, _settings, headers, calls, catalog_run = route_context
    other_response = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog-syncs/{catalog_run.id}",
        headers=headers(other),
    )
    assert other_response.status_code == 404
    legacy_response = await route_client.post(
        f"/api/v1/sources/{source.source_id}/catalog-syncs",
        headers={"X-API-Key": "stage2b-route-test-api-key"},
    )
    assert legacy_response.status_code == 401
    assert [name for name, _owner_id in calls] == ["catalog_run"]


async def test_catalog_sync_routes_require_a_jwt(route_client, route_context):
    _app, _owner, _other, source, _settings, _headers, calls, _catalog_run = route_context

    response = await route_client.get(f"/api/v1/sources/{source.source_id}/catalog")

    assert response.status_code == 401
    assert calls == []


async def test_catalog_sync_request_fails_closed_when_connectors_are_disabled(
    route_client, route_context
):
    _app, owner, _other, source, settings, headers, calls, _catalog_run = route_context
    settings.connectors_enabled = False

    response = await route_client.post(
        f"/api/v1/sources/{source.source_id}/catalog-syncs", headers=headers(owner)
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "connectors_disabled"
    assert calls == []


async def test_catalog_inspection_routes_are_pinned_jwt_only_and_use_safe_shapes(
    route_client, route_context
):
    _app, owner, _other, source, _settings, headers, calls, catalog_run = route_context
    snapshot = catalog_run.catalog_snapshot
    asset = catalog_run.catalog_asset
    owner_headers = headers(owner)

    listed = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/assets",
        params={"snapshot_id": str(snapshot.id)},
        headers=owner_headers,
    )
    assert listed.status_code == 200
    list_body = listed.json()
    assert list_body["snapshot_id"] == str(snapshot.id)
    assert list_body["scope_matches_current_selection"] is True
    assert list_body["items"] == [
        {
            "asset_id": str(asset.asset_id),
            "schema_name": "public",
            "table_name": "orders",
            "table_comment": "<img src=x onerror=alert(1)>",
        }
    ]

    detail = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/assets/{asset.asset_id}",
        params={"snapshot_id": str(snapshot.id)},
        headers=owner_headers,
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert [column["column_name"] for column in detail_body["columns"]] == [
        "tenant_id",
        "customer_id",
    ]
    assert detail_body["declared_keys"][0]["column_names"] == ["tenant_id", "customer_id"]
    assert [item["ordinal_position"] for item in detail_body["foreign_keys"][0]["column_mappings"]] == [
        1,
        2,
    ]

    diff = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/diff",
        params={"snapshot_id": str(snapshot.id)},
        headers=owner_headers,
    )
    assert diff.status_code == 200
    diff_body = diff.json()
    assert diff_body["state"] == "compared"
    assert diff_body["columns_changed"] == [
        {
            "schema_name": "public",
            "table_name": "orders",
            "column_name": "tenant_id",
            "previous_formatted_native_type": "integer",
            "current_formatted_native_type": "bigint",
            "previous_is_nullable": False,
            "current_is_nullable": False,
        }
    ]
    assert [name for name, _owner_id in calls] == [
        "catalog_assets",
        "catalog_asset_detail",
        "catalog_diff",
    ]


async def test_catalog_inspection_routes_reject_other_users_api_key_and_cross_source_ids(
    route_client, route_context
):
    _app, owner, other, source, _settings, headers, calls, catalog_run = route_context
    snapshot = catalog_run.catalog_snapshot
    asset = catalog_run.catalog_asset

    other_response = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/assets/{asset.asset_id}",
        params={"snapshot_id": str(snapshot.id)},
        headers=headers(other),
    )
    assert other_response.status_code == 404
    cross_snapshot = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/assets",
        params={"snapshot_id": str(uuid.uuid4())},
        headers=headers(owner),
    )
    assert cross_snapshot.status_code == 404
    api_key = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/diff",
        params={"snapshot_id": str(snapshot.id)},
        headers={"X-API-Key": "stage2b-route-test-api-key"},
    )
    assert api_key.status_code == 401
    unauthenticated = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/assets",
        params={"snapshot_id": str(snapshot.id)},
    )
    assert unauthenticated.status_code == 401
    missing_snapshot = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/assets",
        headers=headers(owner),
    )
    assert missing_snapshot.status_code == 422
    oversized_page = await route_client.get(
        f"/api/v1/sources/{source.source_id}/catalog/assets",
        params={"snapshot_id": str(snapshot.id), "page": "10001"},
        headers=headers(owner),
    )
    assert oversized_page.status_code == 422
    # JWTs reach the source-scoped services; API-key-only/no-auth requests
    # are rejected before a service call.
    assert [name for name, _owner_id in calls] == [
        "catalog_asset_detail",
        "catalog_assets",
    ]
