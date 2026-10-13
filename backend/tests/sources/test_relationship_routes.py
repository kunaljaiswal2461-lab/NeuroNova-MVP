"""JWT/contract tests for Stage 5B source-scoped relationship APIs."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest

os.environ.setdefault("API_KEY", "stage5b-relationship-route-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.services import relationship_service
from app.services.relationship_service import (
    EligibleDeclaredRelationship,
    RelationshipColumnPair,
    RelationshipDefinitionView,
    ResolvedRelationshipTargetKey,
)
from main import create_app


class _AuthSession:
    def __init__(self, users: dict[uuid.UUID, User]):
        self.users = users

    async def get(self, model, identity):
        return self.users.get(identity) if model is User else None


@pytest.fixture
def relationship_route_context(monkeypatch):
    owner = User(
        id=uuid.uuid4(),
        email="relationship-owner@example.test",
        name="Relationship owner",
        role=UserRole.USER,
        is_active=True,
    )
    other = User(
        id=uuid.uuid4(),
        email="relationship-other@example.test",
        name="Relationship other",
        role=UserRole.USER,
        is_active=True,
    )
    settings = Settings(
        _env_file=None,
        api_key="stage5b-relationship-route-test-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="stage5b-relationship-route-test-jwt-secret",
    )
    source_id = uuid.uuid4()
    relationship_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    foreign_key_id = uuid.uuid4()
    base_asset_id = uuid.uuid4()
    lookup_asset_id = uuid.uuid4()
    base_column_id = uuid.uuid4()
    lookup_column_id = uuid.uuid4()
    target_key_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    pair = RelationshipColumnPair(
        ordinal_position=1,
        base_column_id=base_column_id,
        base_column_name="customer_id",
        lookup_column_id=lookup_column_id,
        lookup_column_name="id",
    )
    target_key = ResolvedRelationshipTargetKey(
        key_id=target_key_id,
        constraint_name="customers_pkey",
        key_kind="primary_key",
        lookup_column_ids=(lookup_column_id,),
        lookup_column_names=("id",),
    )
    view = RelationshipDefinitionView(
        relationship_id=relationship_id,
        version_number=1,
        snapshot_id=snapshot_id,
        foreign_key_id=foreign_key_id,
        base_asset_id=base_asset_id,
        base_schema_name="sales",
        base_table_name="orders",
        lookup_asset_id=lookup_asset_id,
        lookup_schema_name="sales",
        lookup_table_name="customers",
        join_type="left",
        column_pairs=(pair,),
        target_key_id=target_key_id,
        target_key_constraint_name="customers_pkey",
        target_key_kind="primary_key",
        confirmed_by_user_id=owner.id,
        confirmed_at=now,
        state="valid",
        needs_review_reason=None,
        retired_at=None,
    )
    eligible = EligibleDeclaredRelationship(
        snapshot_id=snapshot_id,
        foreign_key_id=foreign_key_id,
        constraint_name="orders_customer_fkey",
        base_asset_id=base_asset_id,
        base_schema_name="sales",
        base_table_name="orders",
        lookup_asset_id=lookup_asset_id,
        lookup_schema_name="sales",
        lookup_table_name="customers",
        column_pairs=(pair,),
        target_key=target_key,
    )
    calls: list[tuple[str, uuid.UUID]] = []

    def _authorized(source: uuid.UUID, actor: uuid.UUID) -> None:
        if source != source_id or actor != owner.id:
            raise NotFoundError("relationship definition not found")

    async def fake_create(_session, *, source_id: uuid.UUID, owner_user_id: uuid.UUID, request):
        _authorized(source_id, owner_user_id)
        assert request.owner_confirmed is True
        calls.append(("create", owner_user_id))
        return view

    async def fake_eligible(_session, *, source_id: uuid.UUID, owner_user_id: uuid.UUID):
        _authorized(source_id, owner_user_id)
        calls.append(("eligible", owner_user_id))
        return (eligible,)

    async def fake_list(_session, *, source_id: uuid.UUID, owner_user_id: uuid.UUID, include_retired: bool):
        _authorized(source_id, owner_user_id)
        assert include_retired is True
        calls.append(("list", owner_user_id))
        return (view,)

    async def fake_get(_session, *, source_id: uuid.UUID, relationship_id: uuid.UUID, owner_user_id: uuid.UUID):
        _authorized(source_id, owner_user_id)
        if relationship_id != view.relationship_id:
            raise NotFoundError("relationship definition not found")
        calls.append(("get", owner_user_id))
        return view

    async def fake_version(
        _session,
        *,
        source_id: uuid.UUID,
        relationship_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        request,
    ):
        _authorized(source_id, owner_user_id)
        assert relationship_id == view.relationship_id
        assert request.owner_confirmed is True
        calls.append(("version", owner_user_id))
        return view

    async def fake_versions(_session, *, source_id: uuid.UUID, relationship_id: uuid.UUID, owner_user_id: uuid.UUID):
        _authorized(source_id, owner_user_id)
        assert relationship_id == view.relationship_id
        calls.append(("versions", owner_user_id))
        return (view,)

    async def fake_retire(_session, *, source_id: uuid.UUID, relationship_id: uuid.UUID, owner_user_id: uuid.UUID):
        _authorized(source_id, owner_user_id)
        assert relationship_id == view.relationship_id
        calls.append(("retire", owner_user_id))
        return view

    monkeypatch.setattr(
        relationship_service, "create_relationship_definition_for_owner", fake_create
    )
    monkeypatch.setattr(
        relationship_service, "list_eligible_declared_relationships_for_owner", fake_eligible
    )
    monkeypatch.setattr(relationship_service, "list_relationship_definitions_for_owner", fake_list)
    monkeypatch.setattr(relationship_service, "get_relationship_definition_for_owner", fake_get)
    monkeypatch.setattr(
        relationship_service, "create_relationship_definition_version_for_owner", fake_version
    )
    monkeypatch.setattr(
        relationship_service,
        "list_relationship_definition_versions_for_owner",
        fake_versions,
    )
    monkeypatch.setattr(
        relationship_service, "retire_relationship_definition_for_owner", fake_retire
    )

    app = create_app()

    async def override_session():
        yield _AuthSession({owner.id: owner, other.id: other})

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings

    def headers(user: User) -> dict[str, str]:
        return {
            "Authorization": "Bearer "
            + create_access_token(
                str(user.id), user.email, user.role.value, settings.jwt_signing_key, 30
            )
        }

    yield {
        "app": app,
        "owner": owner,
        "other": other,
        "source_id": source_id,
        "relationship_id": relationship_id,
        "snapshot_id": snapshot_id,
        "foreign_key_id": foreign_key_id,
        "headers": headers,
        "calls": calls,
    }
    app.dependency_overrides.clear()


def _payload(context: dict[str, object]) -> dict[str, object]:
    return {
        "owner_confirmed": True,
        "snapshot_id": str(context["snapshot_id"]),
        "foreign_key_id": str(context["foreign_key_id"]),
        "join_type": "left",
    }


async def test_relationship_routes_require_jwt_confirmation_and_are_source_scoped(
    relationship_route_context,
):
    context = relationship_route_context
    source_url = f"/api/v1/sources/{context['source_id']}/relationships"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context["app"]), base_url="http://test"
    ) as client:
        assert (await client.post(source_url, json=_payload(context))).status_code == 401
        assert (
            await client.post(
                source_url,
                headers={"X-API-Key": "stage5b-relationship-route-test-key"},
                json=_payload(context),
            )
        ).status_code == 401

        unconfirmed = _payload(context)
        unconfirmed.pop("owner_confirmed")
        assert (
            await client.post(
                source_url,
                headers=context["headers"](context["owner"]),
                json=unconfirmed,
            )
        ).status_code == 422

        created = await client.post(
            source_url,
            headers=context["headers"](context["owner"]),
            json=_payload(context),
        )
        assert created.status_code == 201
        assert created.json()["join_type"] == "left"
        assert created.json()["column_pairs"][0]["base_column_name"] == "customer_id"

        eligible = await client.get(
            f"{source_url}/eligible", headers=context["headers"](context["owner"])
        )
        assert eligible.status_code == 200
        assert eligible.json()["items"][0]["target_key"]["key_kind"] == "primary_key"

        listed = await client.get(source_url, headers=context["headers"](context["owner"]))
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        versioned = await client.post(
            f"{source_url}/{context['relationship_id']}/versions",
            headers=context["headers"](context["owner"]),
            json=_payload(context),
        )
        assert versioned.status_code == 201
        retired = await client.post(
            f"{source_url}/{context['relationship_id']}/retire",
            headers=context["headers"](context["owner"]),
        )
        assert retired.status_code == 200

        denied = await client.get(source_url, headers=context["headers"](context["other"]))
        assert denied.status_code == 404
    assert [name for name, _owner_id in context["calls"]] == [
        "create",
        "eligible",
        "list",
        "version",
        "retire",
    ]
