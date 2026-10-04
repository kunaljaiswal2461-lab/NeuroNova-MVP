"""JWT/contract tests for the Stage 5A source-scoped metric APIs."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest

os.environ.setdefault("API_KEY", "stage5a-metric-route-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.services import metric_service
from app.services.metric_service import MetricDefinitionView
from app.services.postgres_query_plan import PostgresFilter
from main import create_app


class _AuthSession:
    def __init__(self, users: dict[uuid.UUID, User]):
        self.users = users

    async def get(self, model, identity):
        return self.users.get(identity) if model is User else None


@pytest.fixture
def metric_route_context(monkeypatch):
    owner = User(
        id=uuid.uuid4(),
        email="metric-owner@example.test",
        name="Metric owner",
        role=UserRole.USER,
        is_active=True,
    )
    other = User(
        id=uuid.uuid4(),
        email="metric-other@example.test",
        name="Metric other",
        role=UserRole.USER,
        is_active=True,
    )
    settings = Settings(
        _env_file=None,
        api_key="stage5a-metric-route-test-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="stage5a-metric-route-test-jwt-secret",
    )
    source_id = uuid.uuid4()
    metric_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    amount_id = uuid.uuid4()
    status_id = uuid.uuid4()
    event_at_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    view = MetricDefinitionView(
        metric_id=metric_id,
        version_number=1,
        name="Paid order revenue",
        description="Revenue from paid orders",
        snapshot_id=snapshot_id,
        asset_id=asset_id,
        aggregation_operation="sum",
        aggregation_column_id=amount_id,
        aggregation_column_name="amount",
        fixed_filters=(
            PostgresFilter.model_validate(
                {
                    "column_id": status_id,
                    "operator": "eq",
                    "value": {"kind": "text", "value": "paid"},
                }
            ),
        ),
        unit_label="USD",
        default_date_column_id=event_at_id,
        default_date_column_name="event_at",
        default_time_zone="UTC",
        confirmed_by_user_id=owner.id,
        confirmed_at=now,
        state="valid",
        needs_review_reason=None,
        retired_at=None,
    )
    calls: list[tuple[str, uuid.UUID]] = []

    def _authorized(source: uuid.UUID, actor: uuid.UUID) -> None:
        if source != source_id or actor != owner.id:
            raise NotFoundError("metric definition not found")

    async def fake_create(_session, *, source_id: uuid.UUID, owner_user_id: uuid.UUID, request, settings):
        _authorized(source_id, owner_user_id)
        assert request.owner_confirmed is True
        calls.append(("create", owner_user_id))
        return view

    async def fake_list(
        _session,
        *,
        source_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        include_retired: bool,
        settings,
    ):
        _authorized(source_id, owner_user_id)
        assert include_retired is True
        calls.append(("list", owner_user_id))
        return (view,)

    async def fake_get(_session, *, source_id: uuid.UUID, metric_id: uuid.UUID, owner_user_id: uuid.UUID, settings):
        _authorized(source_id, owner_user_id)
        if metric_id != view.metric_id:
            raise NotFoundError("metric definition not found")
        calls.append(("get", owner_user_id))
        return view

    async def fake_version(
        _session,
        *,
        source_id: uuid.UUID,
        metric_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        request,
        settings,
    ):
        _authorized(source_id, owner_user_id)
        assert metric_id == view.metric_id
        assert request.owner_confirmed is True
        calls.append(("version", owner_user_id))
        return view

    async def fake_versions(_session, *, source_id: uuid.UUID, metric_id: uuid.UUID, owner_user_id: uuid.UUID):
        _authorized(source_id, owner_user_id)
        assert metric_id == view.metric_id
        calls.append(("versions", owner_user_id))
        return (view,)

    async def fake_retire(_session, *, source_id: uuid.UUID, metric_id: uuid.UUID, owner_user_id: uuid.UUID):
        _authorized(source_id, owner_user_id)
        assert metric_id == view.metric_id
        calls.append(("retire", owner_user_id))
        return view

    monkeypatch.setattr(metric_service, "create_metric_definition_for_owner", fake_create)
    monkeypatch.setattr(metric_service, "list_metric_definitions_for_owner", fake_list)
    monkeypatch.setattr(metric_service, "get_metric_definition_for_owner", fake_get)
    monkeypatch.setattr(metric_service, "create_metric_definition_version_for_owner", fake_version)
    monkeypatch.setattr(metric_service, "list_metric_definition_versions_for_owner", fake_versions)
    monkeypatch.setattr(metric_service, "retire_metric_definition_for_owner", fake_retire)

    app = create_app()

    async def override_session():
        yield _AuthSession({owner.id: owner, other.id: other})

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings

    def headers(user: User) -> dict[str, str]:
        return {
            "Authorization": "Bearer "
            + create_access_token(
                str(user.id), user.email, user.role.value, settings.jwt_secret_key, 30
            )
        }

    yield {
        "app": app,
        "owner": owner,
        "other": other,
        "source_id": source_id,
        "metric_id": metric_id,
        "snapshot_id": snapshot_id,
        "asset_id": asset_id,
        "amount_id": amount_id,
        "status_id": status_id,
        "event_at_id": event_at_id,
        "headers": headers,
        "calls": calls,
    }
    app.dependency_overrides.clear()


def _payload(context: dict[str, object]) -> dict[str, object]:
    return {
        "name": "Paid order revenue",
        "owner_confirmed": True,
        "description": "Revenue from paid orders",
        "snapshot_id": str(context["snapshot_id"]),
        "asset_id": str(context["asset_id"]),
        "aggregation": {"operation": "sum", "column_id": str(context["amount_id"])},
        "fixed_filters": [
            {
                "column_id": str(context["status_id"]),
                "operator": "eq",
                "value": {"kind": "text", "value": "paid"},
            }
        ],
        "unit_label": "USD",
        "default_date_column_id": str(context["event_at_id"]),
        "default_time_zone": "UTC",
    }


async def test_metric_routes_require_jwt_confirmation_and_are_source_scoped(metric_route_context):
    context = metric_route_context
    source_url = f"/api/v1/sources/{context['source_id']}/metrics"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context["app"]), base_url="http://test"
    ) as client:
        assert (await client.post(source_url, json=_payload(context))).status_code == 401
        # Source metrics are deliberately JWT-only: a legacy global API key
        # cannot create or enumerate an owner's definitions.
        assert (
            await client.post(source_url, headers={"X-API-Key": "stage5a-metric-route-test-key"}, json=_payload(context))
        ).status_code == 401

        unconfirmed = _payload(context)
        unconfirmed.pop("owner_confirmed")
        assert (
            await client.post(source_url, headers=context["headers"](context["owner"]), json=unconfirmed)
        ).status_code == 422

        created = await client.post(
            source_url,
            headers=context["headers"](context["owner"]),
            json=_payload(context),
        )
        assert created.status_code == 201
        assert created.json()["aggregation"] == {
            "operation": "sum",
            "column_id": str(context["amount_id"]),
            "column_name": "amount",
        }
        assert created.json()["state"] == "valid"

        listed = await client.get(source_url, headers=context["headers"](context["owner"]))
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["items"][0]["fixed_filters"][0]["column_id"] == str(context["status_id"])

        versioned = await client.post(
            f"{source_url}/{context['metric_id']}/versions",
            headers=context["headers"](context["owner"]),
            json=_payload(context),
        )
        assert versioned.status_code == 201
        retired = await client.post(
            f"{source_url}/{context['metric_id']}/retire",
            headers=context["headers"](context["owner"]),
        )
        assert retired.status_code == 200

        denied = await client.get(source_url, headers=context["headers"](context["other"]))
        assert denied.status_code == 404
    assert [name for name, _owner_id in context["calls"]] == ["create", "list", "version", "retire"]
