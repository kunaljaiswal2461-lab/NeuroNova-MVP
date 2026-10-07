"""JWT ownership and shared-SSE checks for source-bound database chat."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("API_KEY", "postgres-chat-route-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.api.routes import chat_routes
from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.models import (
    ChatStreamEvent,
    ChatStreamEventType,
    ConversationMode,
    ConversationSession,
)
from main import create_app


class _AuthSession:
    def __init__(self, users):
        self.users = users

    async def get(self, model, identity):
        return self.users.get(identity) if model is User else None


class _SourceRouteFakes:
    def __init__(self, owner_id: uuid.UUID):
        self.owner_id = owner_id
        self.source_id = uuid.uuid4()
        self.session_id = uuid.uuid4()
        self.now = datetime.now(timezone.utc)
        self.created = []
        self.stream_calls = []
        self.active_turn_seen = None

    @property
    def session(self):
        return ConversationSession(
            session_id=self.session_id,
            source_id=self.source_id,
            mode=ConversationMode.AUTO,
            created_at=self.now,
            last_active_at=self.now,
        )

    @property
    def source(self):
        return SimpleNamespace(id=self.source_id, owner_user_id=self.owner_id)

    def _allow(self, source_id, access):
        if (
            source_id != self.source_id
            or access.legacy_api_key
            or access.user_id != self.owner_id
        ):
            raise NotFoundError("source not found")

    async def create_database_session_for_access(self, *, source_id, access, **_kwargs):
        self._allow(source_id, access)
        self.created.append(access)
        return self.session

    async def list_sessions_for_source(self, _db, source_id, *, access, **_kwargs):
        self._allow(source_id, access)
        return [self.session]

    async def get_session_for_access(self, _db, session_id, *, access):
        self._allow(self.source_id if session_id == self.session_id else uuid.uuid4(), access)
        return manager.AuthorizedConversation(session=self.session, source=self.source)

    async def stream_postgres_chat_turn(self, **kwargs):
        self.stream_calls.append(kwargs)
        self.active_turn_seen = await chat_routes._active_database_turn_for_owner(
            turn_id=kwargs["turn_id"],
            session_id=kwargs["session_id"],
            owner_user_id=kwargs["owner_user_id"],
        )
        yield ChatStreamEvent(event=ChatStreamEventType.PLANNING, data={"state": "planning"})
        yield ChatStreamEvent(event=ChatStreamEventType.TOKEN, data={"text": "Safe source reply"})
        yield ChatStreamEvent(event=ChatStreamEventType.DONE, data={})


@pytest.fixture
def source_chat_context(monkeypatch):
    owner = User(
        id=uuid.uuid4(), email="owner@example.test", name="Owner", is_active=True,
        is_verified=True, role=UserRole.USER,
    )
    other = User(
        id=uuid.uuid4(), email="other@example.test", name="Other", is_active=True,
        is_verified=True, role=UserRole.USER,
    )
    settings = Settings(
        api_key="postgres-chat-route-test-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="postgres-chat-route-test-secret",
        data_dir="/tmp/postgres-chat-route-test",
    )
    fakes = _SourceRouteFakes(owner.id)
    application = create_app()

    async def override_session():
        yield _AuthSession({owner.id: owner, other.id: other})

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(
        chat_routes.manager,
        "create_database_session_for_access",
        fakes.create_database_session_for_access,
    )
    monkeypatch.setattr(
        chat_routes.manager,
        "list_sessions_for_source",
        fakes.list_sessions_for_source,
    )
    monkeypatch.setattr(
        chat_routes.manager,
        "get_session_for_access",
        fakes.get_session_for_access,
    )
    monkeypatch.setattr(
        chat_routes,
        "stream_postgres_chat_turn",
        fakes.stream_postgres_chat_turn,
    )
    yield application, settings, owner, other, fakes
    application.dependency_overrides.clear()


def _headers(settings, user):
    token = create_access_token(
        str(user.id), user.email, user.role.value,
        settings.jwt_signing_key, settings.access_token_expire_minutes,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_database_sessions_are_jwt_owner_scoped_and_stream_shared_sse(source_chat_context):
    app, settings, owner, other, fakes = source_chat_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        owner_headers = _headers(settings, owner)
        created = await client.post(
            f"/api/v1/sources/{fakes.source_id}/chat/sessions",
            headers=owner_headers,
            json={"mode": "RAG"},
        )
        assert created.status_code == 201
        assert created.json()["session"]["source_id"] == str(fakes.source_id)
        assert created.json()["session"]["dataset_id"] is None
        assert fakes.created[0].user_id == owner.id

        listed = await client.get(
            f"/api/v1/sources/{fakes.source_id}/chat/sessions", headers=owner_headers
        )
        assert listed.status_code == 200
        assert listed.json()["count"] == 1

        other_result = await client.get(
            f"/api/v1/sources/{fakes.source_id}/chat/sessions",
            headers=_headers(settings, other),
        )
        assert other_result.status_code == 404

        legacy_result = await client.post(
            f"/api/v1/sources/{fakes.source_id}/chat/sessions",
            headers={"X-API-Key": settings.api_key},
            json={},
        )
        assert legacy_result.status_code == 401

        streamed = await client.post(
            f"/api/v1/chat/sessions/{fakes.session_id}/message",
            headers=owner_headers,
            json={"message": "show rows"},
        )
        assert streamed.status_code == 200
        assert "event: planning" in streamed.text
        assert "Safe source reply" in streamed.text
        assert fakes.stream_calls[0]["owner_user_id"] == owner.id
        assert fakes.active_turn_seen is not None
        turn_id = fakes.stream_calls[0]["turn_id"]
        assert await chat_routes._active_database_turn_for_owner(
            turn_id=turn_id,
            session_id=fakes.session_id,
            owner_user_id=owner.id,
        ) is None


@pytest.mark.asyncio
async def test_generic_source_session_endpoint_does_not_disclose_to_legacy_or_other_owner(source_chat_context):
    app, settings, _owner, other, fakes = source_chat_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for headers in (
            _headers(settings, other),
            {"X-API-Key": settings.api_key},
        ):
            response = await client.get(
                f"/api/v1/chat/sessions/{fakes.session_id}", headers=headers
            )
            assert response.status_code == 404


@pytest.mark.asyncio
async def test_owner_can_cancel_only_its_registered_source_turn(source_chat_context):
    app, settings, owner, other, fakes = source_chat_context
    turn_id = uuid.uuid4()
    cancellation = chat_routes.PostgresExecutionCancellation()
    await chat_routes._register_database_turn(
        turn_id=turn_id,
        session_id=fakes.session_id,
        owner_user_id=owner.id,
        cancellation=cancellation,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rejected = await client.post(
                f"/api/v1/chat/sessions/{fakes.session_id}/turns/{turn_id}/cancel",
                headers=_headers(settings, other),
            )
            assert rejected.status_code == 404
            assert not cancellation.cancelled

            accepted = await client.post(
                f"/api/v1/chat/sessions/{fakes.session_id}/turns/{turn_id}/cancel",
                headers=_headers(settings, owner),
            )
            assert accepted.status_code == 202
            assert cancellation.cancelled

            missing = await client.post(
                f"/api/v1/chat/sessions/{fakes.session_id}/turns/{uuid.uuid4()}/cancel",
                headers=_headers(settings, owner),
            )
            assert missing.status_code == 404
    finally:
        await chat_routes._unregister_database_turn(turn_id)


@pytest.mark.asyncio
async def test_owner_can_cancel_a_live_turn_after_its_source_session_is_deleted(
    monkeypatch, source_chat_context
):
    app, settings, owner, _other, fakes = source_chat_context
    turn_id = uuid.uuid4()
    cancellation = chat_routes.PostgresExecutionCancellation()

    async def deleted_session(*_args, **_kwargs):
        raise NotFoundError("conversation session not found")

    monkeypatch.setattr(chat_routes.manager, "get_session_for_access", deleted_session)
    await chat_routes._register_database_turn(
        turn_id=turn_id,
        session_id=fakes.session_id,
        owner_user_id=owner.id,
        cancellation=cancellation,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{fakes.session_id}/turns/{turn_id}/cancel",
                headers=_headers(settings, owner),
            )
        assert response.status_code == 202
        assert cancellation.cancelled
    finally:
        await chat_routes._unregister_database_turn(turn_id)
