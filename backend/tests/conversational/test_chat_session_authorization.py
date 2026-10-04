"""HTTP and manager-level ownership checks for file-dataset chat sessions."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

# ``main`` builds a settings object at import time, so seed its required values
# before importing the application factory.
os.environ.setdefault("API_KEY", "chat-session-auth-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.api.routes import chat_routes
from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.conversation import ConversationModeDB, ConversationSessionRow
from app.db.models.dataset import DatasetRecord, DatasetStatus, FileType
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.models import (
    ChatMessage,
    ChatRole,
    ChatStreamEvent,
    ChatStreamEventType,
    ConversationMode,
    ConversationSession,
    QueryColumn,
    QueryResult,
)
from main import create_app


@dataclass
class _RouteFakes:
    owner_id: uuid.UUID
    dataset_id: uuid.UUID
    session_id: uuid.UUID
    now: datetime
    calls: list[tuple[str, manager.ConversationAccess]] = field(default_factory=list)
    created: list[manager.ConversationAccess] = field(default_factory=list)
    deleted: list[manager.ConversationAccess] = field(default_factory=list)
    stream_calls: list[dict] = field(default_factory=list)

    @property
    def session(self) -> ConversationSession:
        return ConversationSession(
            session_id=self.session_id,
            dataset_id=self.dataset_id,
            mode=ConversationMode.AUTO,
            created_at=self.now,
            last_active_at=self.now,
            message_count=1,
        )

    @property
    def dataset(self):
        return SimpleNamespace(
            id=self.dataset_id,
            user_id=self.owner_id,
            status=DatasetStatus.COMPLETE,
        )

    @property
    def conversation(self) -> manager.AuthorizedConversation:
        return manager.AuthorizedConversation(
            session=self.session,
            dataset=self.dataset,
        )

    def _allow_dataset(
        self, dataset_id: uuid.UUID, access: manager.ConversationAccess
    ) -> None:
        self.calls.append(("dataset", access))
        if (
            dataset_id != self.dataset_id
            or access.legacy_api_key
            or access.user_id != self.owner_id
        ):
            raise NotFoundError("dataset not found")

    def _allow_session(
        self, session_id: uuid.UUID, access: manager.ConversationAccess
    ) -> None:
        self.calls.append(("session", access))
        if (
            session_id != self.session_id
            or access.legacy_api_key
            or access.user_id != self.owner_id
        ):
            raise NotFoundError("conversation session not found")

    async def create_session_for_access(
        self, *, dataset_id, access, **_kwargs
    ) -> ConversationSession:
        self._allow_dataset(dataset_id, access)
        self.created.append(access)
        return self.session

    async def list_sessions_for_dataset(
        self, _db, dataset_id, *, access, **_kwargs
    ) -> list[ConversationSession]:
        self._allow_dataset(dataset_id, access)
        return [self.session]

    async def get_session_for_access(
        self, _db, session_id, *, access
    ) -> manager.AuthorizedConversation:
        self._allow_session(session_id, access)
        return self.conversation

    async def delete_session(self, _db, session_id, *, access) -> None:
        self._allow_session(session_id, access)
        self.deleted.append(access)

    async def get_recent_messages_for_access(
        self, _db, session_id, *, access, **_kwargs
    ) -> tuple[manager.AuthorizedConversation, list[ChatMessage]]:
        conversation = await self.get_session_for_access(_db, session_id, access=access)
        return conversation, [
            ChatMessage(
                message_id=uuid.uuid4(),
                session_id=self.session_id,
                role=ChatRole.ASSISTANT,
                content="Owner-only result",
                created_at=self.now,
                query_result=QueryResult(
                    expression="df.select('private_value')",
                    columns=[QueryColumn(name="private_value", dtype="Utf8")],
                    rows=[["secret result"]],
                    row_count=1,
                ),
            )
        ]

    async def stream_chat_turn(self, **kwargs):
        self.stream_calls.append(kwargs)
        yield ChatStreamEvent(
            event=ChatStreamEventType.TOKEN,
            data={"text": "Owner-only streamed answer"},
        )
        yield ChatStreamEvent(event=ChatStreamEventType.DONE, data={})


class _AuthSession:
    """Only implements the user lookup needed by the production JWT dependency."""

    def __init__(self, users: dict[uuid.UUID, User]):
        self.users = users

    async def get(self, model, identity):
        if model is User:
            return self.users.get(identity)
        return None


@dataclass
class _ChatTestContext:
    app: object
    settings: Settings
    fakes: _RouteFakes
    owner: User
    other: User

    def headers_for(self, user: User) -> dict[str, str]:
        token = create_access_token(
            str(user.id),
            user.email,
            user.role.value,
            self.settings.jwt_secret_key,
            self.settings.access_token_expire_minutes,
        )
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def chat_test_context(monkeypatch) -> _ChatTestContext:
    owner = User(
        id=uuid.uuid4(),
        email="owner@example.test",
        name="Owner",
        is_active=True,
        is_verified=True,
        role=UserRole.USER,
    )
    other = User(
        id=uuid.uuid4(),
        email="other@example.test",
        name="Other",
        is_active=True,
        is_verified=True,
        role=UserRole.USER,
    )
    settings = Settings(
        api_key="chat-session-auth-test-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="chat-session-auth-test-secret",
        data_dir="/tmp/chat-session-auth-test",
    )
    fakes = _RouteFakes(
        owner_id=owner.id,
        dataset_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        now=datetime.now(timezone.utc),
    )
    fake_session = _AuthSession({owner.id: owner, other.id: other})
    application = create_app()

    async def override_session():
        yield fake_session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_settings] = lambda: settings

    monkeypatch.setattr(
        chat_routes.manager,
        "create_session_for_access",
        fakes.create_session_for_access,
    )
    monkeypatch.setattr(
        chat_routes.manager,
        "list_sessions_for_dataset",
        fakes.list_sessions_for_dataset,
    )
    monkeypatch.setattr(
        chat_routes.manager,
        "get_session_for_access",
        fakes.get_session_for_access,
    )
    monkeypatch.setattr(
        chat_routes.manager,
        "delete_session",
        fakes.delete_session,
    )
    monkeypatch.setattr(
        chat_routes.manager,
        "get_recent_messages_for_access",
        fakes.get_recent_messages_for_access,
    )
    monkeypatch.setattr(chat_routes, "stream_chat_turn", fakes.stream_chat_turn)

    yield _ChatTestContext(application, settings, fakes, owner, other)
    application.dependency_overrides.clear()


@pytest.fixture
async def chat_client(chat_test_context):
    transport = httpx.ASGITransport(app=chat_test_context.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_owner_can_read_send_and_delete_own_session(
    chat_client, chat_test_context
):
    headers = chat_test_context.headers_for(chat_test_context.owner)
    dataset_id = chat_test_context.fakes.dataset_id
    session_id = chat_test_context.fakes.session_id

    listed = await chat_client.get(
        f"/api/v1/datasets/{dataset_id}/chat/sessions", headers=headers
    )
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    fetched = await chat_client.get(
        f"/api/v1/chat/sessions/{session_id}", headers=headers
    )
    assert fetched.status_code == 200

    transcript = await chat_client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=headers
    )
    assert transcript.status_code == 200
    assert transcript.json()["messages"][0]["query_result"]["rows"] == [["secret result"]]

    streamed = await chat_client.post(
        f"/api/v1/chat/sessions/{session_id}/message",
        headers=headers,
        json={"message": "Summarize the private result"},
    )
    assert streamed.status_code == 200
    assert "Owner-only streamed answer" in streamed.text
    assert len(chat_test_context.fakes.stream_calls) == 1
    stream_call = chat_test_context.fakes.stream_calls[0]
    assert stream_call["authorized_session"].session_id == session_id
    assert stream_call["authorized_dataset"].id == dataset_id

    deleted = await chat_client.delete(
        f"/api/v1/chat/sessions/{session_id}", headers=headers
    )
    assert deleted.status_code == 204
    assert chat_test_context.fakes.deleted[0].user_id == chat_test_context.owner.id


@pytest.mark.parametrize(
    ("method", "path_kind", "payload"),
    [
        ("GET", "dataset_sessions", None),
        ("GET", "session", None),
        ("GET", "messages", None),
        ("DELETE", "session", None),
        ("POST", "message", {"message": "steal this result"}),
    ],
)
async def test_other_user_cannot_access_or_operate_on_owner_session(
    chat_client, chat_test_context, method, path_kind, payload
):
    dataset_id = chat_test_context.fakes.dataset_id
    session_id = chat_test_context.fakes.session_id
    paths = {
        "dataset_sessions": f"/api/v1/datasets/{dataset_id}/chat/sessions",
        "session": f"/api/v1/chat/sessions/{session_id}",
        "messages": f"/api/v1/chat/sessions/{session_id}/messages",
        "message": f"/api/v1/chat/sessions/{session_id}/message",
    }

    response = await chat_client.request(
        method,
        paths[path_kind],
        headers=chat_test_context.headers_for(chat_test_context.other),
        json=payload,
    )

    assert response.status_code == 404
    assert "secret result" not in response.text
    assert not chat_test_context.fakes.deleted
    assert not chat_test_context.fakes.stream_calls


async def test_session_creation_requires_access_to_the_dataset(
    chat_client, chat_test_context
):
    dataset_id = chat_test_context.fakes.dataset_id

    owner_created = await chat_client.post(
        f"/api/v1/datasets/{dataset_id}/chat/sessions",
        headers=chat_test_context.headers_for(chat_test_context.owner),
        json={"mode": "AUTO"},
    )
    assert owner_created.status_code == 201
    assert chat_test_context.fakes.created == [
        manager.ConversationAccess.for_user(chat_test_context.owner.id)
    ]

    other_created = await chat_client.post(
        f"/api/v1/datasets/{dataset_id}/chat/sessions",
        headers=chat_test_context.headers_for(chat_test_context.other),
        json={"mode": "AUTO"},
    )
    assert other_created.status_code == 404
    assert len(chat_test_context.fakes.created) == 1


@pytest.mark.parametrize(
    ("method", "path_kind", "payload"),
    [
        ("POST", "create", {"mode": "AUTO"}),
        ("GET", "dataset_sessions", None),
        ("GET", "session", None),
        ("GET", "messages", None),
        ("DELETE", "session", None),
        ("POST", "message", {"message": "hello"}),
    ],
)
async def test_chat_session_routes_reject_unauthenticated_requests(
    chat_client, chat_test_context, method, path_kind, payload
):
    dataset_id = chat_test_context.fakes.dataset_id
    session_id = chat_test_context.fakes.session_id
    paths = {
        "create": f"/api/v1/datasets/{dataset_id}/chat/sessions",
        "dataset_sessions": f"/api/v1/datasets/{dataset_id}/chat/sessions",
        "session": f"/api/v1/chat/sessions/{session_id}",
        "messages": f"/api/v1/chat/sessions/{session_id}/messages",
        "message": f"/api/v1/chat/sessions/{session_id}/message",
    }

    response = await chat_client.request(method, paths[path_kind], json=payload)

    assert response.status_code == 401
    assert not chat_test_context.fakes.stream_calls


async def test_legacy_api_key_cannot_access_jwt_owned_conversation_resources_or_start_sse(
    chat_client, chat_test_context
):
    dataset_id = chat_test_context.fakes.dataset_id
    session_id = chat_test_context.fakes.session_id
    headers = {"X-API-Key": chat_test_context.settings.api_key}

    created = await chat_client.post(
        f"/api/v1/datasets/{dataset_id}/chat/sessions",
        headers=headers,
        json={"mode": "AUTO"},
    )
    assert created.status_code == 404

    listed = await chat_client.get(
        f"/api/v1/datasets/{dataset_id}/chat/sessions", headers=headers
    )
    assert listed.status_code == 404

    fetched = await chat_client.get(
        f"/api/v1/chat/sessions/{session_id}", headers=headers
    )
    assert fetched.status_code == 404

    streamed = await chat_client.post(
        f"/api/v1/chat/sessions/{session_id}/message",
        headers=headers,
        json={"message": "steal this result"},
    )
    assert streamed.status_code == 404
    assert not chat_test_context.fakes.stream_calls


@pytest.mark.parametrize(
    "authorization",
    ["Bearer", "Basic ignored", "Bearer-not-a-token", "Bearer invalid-token"],
)
async def test_malformed_authorization_cannot_downgrade_to_legacy_sse_access(
    chat_client, chat_test_context, authorization
):
    """An Authorization header is authoritative even when a valid key is sent."""
    response = await chat_client.post(
        f"/api/v1/chat/sessions/{chat_test_context.fakes.session_id}/message",
        headers={
            "Authorization": authorization,
            "X-API-Key": chat_test_context.settings.api_key,
        },
        json={"message": "do not start a stream"},
    )

    assert response.status_code == 401
    assert not chat_test_context.fakes.calls
    assert not chat_test_context.fakes.stream_calls


async def test_jwt_takes_precedence_over_a_valid_legacy_api_key(
    chat_client, chat_test_context
):
    token = chat_test_context.headers_for(chat_test_context.owner)["Authorization"]
    headers = {
        # Authentication schemes are case-insensitive; preserve the JWT actor
        # rather than falling through to the less-privileged key namespace.
        "Authorization": token.replace("Bearer ", "bearer ", 1),
        "X-API-Key": chat_test_context.settings.api_key,
    }

    response = await chat_client.get(
        f"/api/v1/chat/sessions/{chat_test_context.fakes.session_id}",
        headers=headers,
    )

    assert response.status_code == 200
    assert chat_test_context.fakes.calls == [
        ("session", manager.ConversationAccess.for_user(chat_test_context.owner.id))
    ]


class _OwnershipSession:
    """Minimal async session for manager ownership checks without a live DB."""

    def __init__(self, conversation, dataset):
        self.conversation = conversation
        self.dataset = dataset
        self.deleted = []
        self.commits = 0

    async def get(self, model, identity):
        if model is ConversationSessionRow and identity == self.conversation.id:
            return self.conversation
        if model is DatasetRecord and identity == self.dataset.id:
            return self.dataset
        return None

    async def delete(self, row):
        self.deleted.append(row)

    async def commit(self):
        self.commits += 1


class _OwnershipListSession(_OwnershipSession):
    def __init__(self, conversation, dataset):
        super().__init__(conversation, dataset)
        self.executed = 0

    async def execute(self, _statement):
        self.executed += 1

        class _Scalars:
            def all(inner_self):
                return [self.conversation]

        class _Result:
            def scalars(inner_self):
                return _Scalars()

        return _Result()


def _ownership_rows(*, dataset_owner, session_owner):
    now = datetime.now(timezone.utc)
    dataset_id = uuid.uuid4()
    dataset = DatasetRecord(
        id=dataset_id,
        filename="owned.csv",
        original_name="owned.csv",
        file_type=FileType.CSV,
        size_bytes=1,
        status=DatasetStatus.COMPLETE,
        user_id=dataset_owner,
    )
    conversation = ConversationSessionRow(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        mode=ConversationModeDB.AUTO,
        user_id=session_owner,
        created_at=now,
        updated_at=now,
        last_active_at=now,
        message_count=0,
    )
    return conversation, dataset


async def test_manager_enforces_the_same_owner_policy_for_read_and_delete():
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    conversation, dataset = _ownership_rows(
        dataset_owner=owner_id, session_owner=owner_id
    )
    db = _OwnershipSession(conversation, dataset)

    authorized = await manager.get_session_for_access(
        db, conversation.id, access=manager.ConversationAccess.for_user(owner_id)
    )
    assert authorized.session.session_id == conversation.id

    with pytest.raises(NotFoundError):
        await manager.get_session_for_access(
            db, conversation.id, access=manager.ConversationAccess.for_user(other_id)
        )
    with pytest.raises(NotFoundError):
        await manager.delete_session(
            db, conversation.id, access=manager.ConversationAccess.for_legacy_api_key()
        )
    assert not db.deleted

    await manager.delete_session(
        db, conversation.id, access=manager.ConversationAccess.for_user(owner_id)
    )
    assert db.deleted == [conversation]
    assert db.commits == 1


async def test_manager_authorizes_dataset_before_creating_or_listing_sessions(monkeypatch):
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    conversation, dataset = _ownership_rows(
        dataset_owner=owner_id, session_owner=owner_id
    )
    db = _OwnershipListSession(conversation, dataset)
    created: list[dict] = []

    async def fake_create_session(**kwargs):
        created.append(kwargs)
        return manager._row_to_session(conversation)

    monkeypatch.setattr(manager, "create_session", fake_create_session)

    created_session = await manager.create_session_for_access(
        session_db=db,
        dataset_id=dataset.id,
        access=manager.ConversationAccess.for_user(owner_id),
    )
    assert created_session.session_id == conversation.id
    assert created[0]["user_id"] == owner_id

    visible = await manager.list_sessions_for_dataset(
        db,
        dataset.id,
        access=manager.ConversationAccess.for_user(owner_id),
    )
    assert [item.session_id for item in visible] == [conversation.id]
    assert db.executed == 1

    with pytest.raises(NotFoundError):
        await manager.create_session_for_access(
            session_db=db,
            dataset_id=dataset.id,
            access=manager.ConversationAccess.for_user(other_id),
        )
    with pytest.raises(NotFoundError):
        await manager.list_sessions_for_dataset(
            db,
            dataset.id,
            access=manager.ConversationAccess.for_legacy_api_key(),
        )
    assert len(created) == 1
    assert db.executed == 1


async def test_manager_legacy_scope_requires_unowned_dataset_and_session():
    legacy_conversation, legacy_dataset = _ownership_rows(
        dataset_owner=None, session_owner=None
    )
    legacy_db = _OwnershipSession(legacy_conversation, legacy_dataset)
    legacy = await manager.get_session_for_access(
        legacy_db,
        legacy_conversation.id,
        access=manager.ConversationAccess.for_legacy_api_key(),
    )
    assert legacy.session.session_id == legacy_conversation.id

    owner_id = uuid.uuid4()
    historic_conversation, owned_dataset = _ownership_rows(
        dataset_owner=owner_id, session_owner=None
    )
    owned_db = _OwnershipSession(historic_conversation, owned_dataset)
    owner = await manager.get_session_for_access(
        owned_db,
        historic_conversation.id,
        access=manager.ConversationAccess.for_user(owner_id),
    )
    assert owner.session.session_id == historic_conversation.id
    with pytest.raises(NotFoundError):
        await manager.get_session_for_access(
            owned_db,
            historic_conversation.id,
            access=manager.ConversationAccess.for_legacy_api_key(),
        )


async def test_manager_fails_closed_for_conflicting_session_and_dataset_owners():
    dataset_owner = uuid.uuid4()
    session_owner = uuid.uuid4()
    conversation, dataset = _ownership_rows(
        dataset_owner=dataset_owner,
        session_owner=session_owner,
    )
    db = _OwnershipSession(conversation, dataset)

    for access in (
        manager.ConversationAccess.for_user(dataset_owner),
        manager.ConversationAccess.for_user(session_owner),
        manager.ConversationAccess.for_legacy_api_key(),
    ):
        with pytest.raises(NotFoundError):
            await manager.get_session_for_access(
                db, conversation.id, access=access
            )

    assert not db.deleted
