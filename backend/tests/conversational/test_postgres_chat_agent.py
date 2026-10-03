"""Stage 4C orchestration tests without a live PostgreSQL source."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.services.postgres_query_executor import PostgresQueryExecutionResult
from app.services.postgres_query_plan import PostgresQueryPlan
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational import postgres_chat_agent
from agentic_engine.conversational.models import (
    ChatMessage,
    ChatRole,
    ChatStreamEventType,
    ConversationMode,
    ConversationSession,
    PostgresChatResult,
)
from agentic_engine.conversational.postgres_planner import PostgresPlanningResult


class _Session:
    async def rollback(self):
        return None


def _execution(*, source_id, snapshot_id, status="succeeded", failure_code=None):
    now = datetime.now(timezone.utc)
    return PostgresQueryExecutionResult(
        execution_id=uuid.uuid4(),
        status=status,
        source_id=source_id,
        snapshot_id=snapshot_id,
        asset_id=uuid.uuid4(),
        failure_code=failure_code,
        started_at=now,
        completed_at=now,
        duration_ms=2.5,
        complete=status == "succeeded",
    )


async def _install_manager(monkeypatch, *, session, source):
    saved = []

    async def append_message(*, session_id, role, content, postgres_result=None, **kwargs):
        normalized = (
            PostgresChatResult.model_validate(postgres_result.model_dump(mode="python"))
            if postgres_result is not None
            else None
        )
        message = ChatMessage(
            message_id=uuid.uuid4(),
            session_id=session_id,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
            mode_used=kwargs.get("mode_used"),
            postgres_result=normalized,
            degraded=kwargs.get("degraded", False),
            degraded_reason=kwargs.get("degraded_reason"),
            unsupported=kwargs.get("unsupported", False),
            token_usage=kwargs.get("token_usage"),
        )
        saved.append(message)
        return message

    async def recent_messages(*_args, **_kwargs):
        return list(saved)

    async def authorized(*_args, **_kwargs):
        return manager.AuthorizedConversation(session=session, source=source)

    monkeypatch.setattr(postgres_chat_agent.manager, "append_message", append_message)
    monkeypatch.setattr(postgres_chat_agent.manager, "get_recent_messages", recent_messages)
    monkeypatch.setattr(postgres_chat_agent.manager, "get_session_for_access", authorized)
    return saved


@pytest.mark.asyncio
async def test_database_turn_pins_plan_and_uses_stage4b_gateway(monkeypatch, settings_with_key):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(
        asset_id=asset_id,
        projections=({"column_id": column_id},),
        limit=5,
    )

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan", message="ready", snapshot_id=snapshot_id, plan=plan
        )

    calls = []

    async def fake_executor(factory, **kwargs):
        calls.append((factory, kwargs))
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="show amount",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
        )
    ]

    assert [event.event for event in events] == [
        ChatStreamEventType.START,
        ChatStreamEventType.PLANNING,
        ChatStreamEventType.EXECUTING,
        ChatStreamEventType.POSTGRES_RESULT,
        ChatStreamEventType.TOKEN,
        ChatStreamEventType.DONE,
    ]
    assert calls[0][1]["owner_user_id"] == owner_id
    assert calls[0][1]["source_id"] == source_id
    assert calls[0][1]["snapshot_id"] == snapshot_id
    assert calls[0][1]["plan"] == plan
    assert events[3].data["postgres_result"]["snapshot_id"] == str(snapshot_id)
    assert saved[0].role is ChatRole.USER
    assert saved[-1].postgres_result is not None


@pytest.mark.asyncio
async def test_failed_database_result_does_not_fabricate_an_answer(monkeypatch, settings_with_key):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(
        asset_id=asset_id,
        projections=({"column_id": column_id},),
        limit=5,
    )

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan", message="ready", snapshot_id=snapshot_id, plan=plan
        )

    async def fake_executor(*_args, **_kwargs):
        return _execution(
            source_id=source_id,
            snapshot_id=snapshot_id,
            status="failed",
            failure_code="schema_changed",
        )

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="show amount",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
        )
    ]

    result_event = next(event for event in events if event.event is ChatStreamEventType.POSTGRES_RESULT)
    reply = next(event.data["text"] for event in events if event.event is ChatStreamEventType.TOKEN)
    assert result_event.data["postgres_result"]["failure_code"] == "schema_changed"
    assert "cannot provide an answer" in reply
    assert "schema_changed" not in reply


@pytest.mark.asyncio
async def test_cancellation_after_executor_return_never_persists_or_emits_success_rows(
    monkeypatch, settings_with_key
):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(asset_id=asset_id, projections=({"column_id": column_id},), limit=5)
    cancellation = PostgresExecutionCancellation()

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan", message="ready", snapshot_id=snapshot_id, plan=plan
        )

    async def fake_executor(*_args, **kwargs):
        # The executor finished, but cancellation wins before the agent can
        # authorize, retain, or disclose the returned result.
        await kwargs["cancellation"].cancel()
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="show amount",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
            cancellation=cancellation,
        )
    ]

    assert ChatStreamEventType.POSTGRES_RESULT not in [event.event for event in events]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "execution_cancelled"
    assert "cancelled" in saved[-1].content.lower()


@pytest.mark.asyncio
async def test_cancellation_after_planning_short_circuits_before_stage4b(monkeypatch, settings_with_key):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(asset_id=asset_id, projections=({"column_id": column_id},), limit=5)
    cancellation = PostgresExecutionCancellation()
    executor_called = False

    async def fake_plan(**_kwargs):
        await cancellation.cancel()
        return PostgresPlanningResult(
            kind="plan", message="ready", snapshot_id=snapshot_id, plan=plan
        )

    async def fake_executor(*_args, **_kwargs):
        nonlocal executor_called
        executor_called = True
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="show amount",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
            cancellation=cancellation,
        )
    ]

    assert not executor_called
    assert ChatStreamEventType.EXECUTING not in [event.event for event in events]
    assert saved[-1].degraded_reason == "execution_cancelled"


@pytest.mark.asyncio
async def test_stream_close_after_start_persists_one_safe_cancellation_turn(
    monkeypatch, settings_with_key
):
    owner_id, source_id = uuid.uuid4(), uuid.uuid4()
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)

    @asynccontextmanager
    async def factory():
        yield _Session()

    stream = postgres_chat_agent.stream_postgres_chat_turn(
        session_id=session.session_id,
        user_message="show amount",
        session_db=_Session(),
        owner_user_id=owner_id,
        settings=settings_with_key,
        authorized_session=session,
        authorized_source=source,
        session_factory=factory,
    )

    first = await anext(stream)
    assert first.event is ChatStreamEventType.START
    await stream.aclose()
    # ``aclose`` runs the generator's finalizer, including its shielded fresh
    # session cleanup. Let that task settle before observing the transcript.
    await asyncio.sleep(0)

    assert [message.role for message in saved] == [ChatRole.USER, ChatRole.ASSISTANT]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "execution_cancelled"


@pytest.mark.asyncio
async def test_task_cancellation_while_planning_repairs_the_committed_user_turn(
    monkeypatch, settings_with_key
):
    owner_id, source_id = uuid.uuid4(), uuid.uuid4()
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    planning_started = asyncio.Event()
    keep_planning = asyncio.Event()

    async def blocked_plan(**_kwargs):
        planning_started.set()
        await keep_planning.wait()
        raise AssertionError("the cancelled planner must not resume")

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", blocked_plan)

    async def consume():
        async for _event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="show amount",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            session_factory=factory,
        ):
            pass

    task = asyncio.create_task(consume())
    await planning_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert [message.role for message in saved] == [ChatRole.USER, ChatRole.ASSISTANT]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "execution_cancelled"
