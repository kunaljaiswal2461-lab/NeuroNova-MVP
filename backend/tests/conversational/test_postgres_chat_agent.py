"""Stage 4C orchestration tests without a live PostgreSQL source."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.services.postgres_query_executor import (
    PostgresMetricProvenance,
    PostgresQueryExecutionResult,
    PostgresRelationshipProvenance,
)
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


def _execution(
    *,
    source_id,
    snapshot_id,
    status="succeeded",
    failure_code=None,
    complete=None,
    returned_row_count=0,
    safety_byte_truncated=False,
):
    now = datetime.now(timezone.utc)
    return PostgresQueryExecutionResult(
        execution_id=uuid.uuid4(),
        status=status,
        source_id=source_id,
        snapshot_id=snapshot_id,
        asset_id=uuid.uuid4(),
        failure_code=failure_code,
        returned_row_count=returned_row_count,
        complete=(status == "succeeded" if complete is None else complete),
        safety_byte_truncated=safety_byte_truncated,
        started_at=now,
        completed_at=now,
        duration_ms=2.5,
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


def test_byte_truncated_zero_row_result_is_not_described_as_no_match():
    result = _execution(
        source_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        complete=False,
        returned_row_count=0,
        safety_byte_truncated=True,
    )

    summary = postgres_chat_agent._summary_for_result(result)

    assert "no rows matched" not in summary.lower()
    assert "no result rows could be safely displayed" in summary.lower()
    assert "incomplete" in summary.lower()


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


@pytest.mark.asyncio
async def test_task_cancellation_while_persisting_cancellation_repairs_the_user_turn(
    monkeypatch, settings_with_key
):
    """An interrupted cancellation write must still receive fresh-session cleanup."""

    class BlockingRollbackSession(_Session):
        def __init__(self):
            self.rollback_started = asyncio.Event()
            self.release_rollback = asyncio.Event()

        async def rollback(self):
            self.rollback_started.set()
            await self.release_rollback.wait()

    owner_id, source_id = uuid.uuid4(), uuid.uuid4()
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    request_session = BlockingRollbackSession()
    cancellation = PostgresExecutionCancellation()

    @asynccontextmanager
    async def factory():
        yield _Session()

    stream = postgres_chat_agent.stream_postgres_chat_turn(
        session_id=session.session_id,
        user_message="show amount",
        session_db=request_session,
        owner_user_id=owner_id,
        settings=settings_with_key,
        authorized_session=session,
        authorized_source=source,
        session_factory=factory,
        cancellation=cancellation,
    )
    first = await anext(stream)
    assert first.event is ChatStreamEventType.START

    await cancellation.cancel()
    waiting_for_cancelled_terminal = asyncio.create_task(anext(stream))
    await asyncio.wait_for(request_session.rollback_started.wait(), timeout=1)
    waiting_for_cancelled_terminal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_for_cancelled_terminal
    await asyncio.sleep(0)

    assert [message.role for message in saved] == [ChatRole.USER, ChatRole.ASSISTANT]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "execution_cancelled"


@pytest.mark.asyncio
async def test_access_change_after_execution_is_not_relabelled_as_cancellation(
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

    async def fake_executor(*_args, **_kwargs):
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    async def no_longer_authorized(**_kwargs):
        return False

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    monkeypatch.setattr(postgres_chat_agent, "_authorize_before_disclosure", no_longer_authorized)
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

    assert [event.event for event in events][-2:] == [
        ChatStreamEventType.ERROR,
        ChatStreamEventType.DONE,
    ]
    assert cancellation.cancelled is False
    assert [message.role for message in saved] == [ChatRole.USER]


@pytest.mark.asyncio
async def test_result_persistence_failure_records_a_safe_failure_not_cancellation(
    monkeypatch, settings_with_key
):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(asset_id=asset_id, projections=({"column_id": column_id},), limit=5)
    original_append = postgres_chat_agent.manager.append_message
    result_write_failed = False

    async def fail_only_result_write(*args, **kwargs):
        nonlocal result_write_failed
        if (
            kwargs.get("role") is ChatRole.ASSISTANT
            and kwargs.get("postgres_result") is not None
            and not result_write_failed
        ):
            result_write_failed = True
            raise RuntimeError("simulated result persistence failure")
        return await original_append(*args, **kwargs)

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan", message="ready", snapshot_id=snapshot_id, plan=plan
        )

    async def fake_executor(*_args, **_kwargs):
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent.manager, "append_message", fail_only_result_write)
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

    assert ChatStreamEventType.POSTGRES_RESULT not in [event.event for event in events]
    assert any(event.event is ChatStreamEventType.ERROR for event in events)
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "result_persistence_failed"
    assert all(message.degraded_reason != "execution_cancelled" for message in saved)


@pytest.mark.asyncio
async def test_changed_metric_pin_blocks_execution_before_gateway(monkeypatch, settings_with_key):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(asset_id=asset_id, projections=({"column_id": column_id},), limit=5)
    metric = SimpleNamespace(
        metric_id=uuid.uuid4(),
        metric_version_id=uuid.uuid4(),
        version_number=1,
        snapshot_id=snapshot_id,
    )
    executor_called = False

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan",
            message="ready",
            snapshot_id=snapshot_id,
            plan=plan,
            metric_definition=metric,
        )

    async def pin_is_invalid(**_kwargs):
        return False

    async def fake_executor(*_args, **_kwargs):
        nonlocal executor_called
        executor_called = True
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    monkeypatch.setattr(postgres_chat_agent, "_verify_metric_pin", pin_is_invalid)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="Show net revenue",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
        )
    ]

    assert not executor_called
    assert ChatStreamEventType.EXECUTING not in [event.event for event in events]
    assert ChatStreamEventType.POSTGRES_RESULT not in [event.event for event in events]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "metric_pin_invalid"
    assert "changed or needs review" in saved[-1].content


@pytest.mark.asyncio
async def test_metric_execution_passes_gateway_bound_fresh_pin_verifier(monkeypatch, settings_with_key):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(asset_id=asset_id, projections=({"column_id": column_id},), limit=5)
    metric = SimpleNamespace(
        metric_id=uuid.uuid4(),
        metric_version_id=uuid.uuid4(),
        version_number=1,
        snapshot_id=snapshot_id,
    )
    pin_checks = 0

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan",
            message="ready",
            snapshot_id=snapshot_id,
            plan=plan,
            metric_definition=metric,
        )

    async def pin_is_current_only_for_precheck(**_kwargs):
        nonlocal pin_checks
        pin_checks += 1
        return pin_checks == 1

    async def fake_executor(*_args, **kwargs):
        assert kwargs["metric_definition_version_id"] == metric.metric_version_id
        assert await kwargs["metric_pin_verifier"]() is False
        # This models the gateway's safe no-remote failure after its immediate
        # pre-gateway pin check fails.
        return _execution(
            source_id=source_id,
            snapshot_id=snapshot_id,
            status="failed",
            failure_code="metric_definition_changed",
        )

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    monkeypatch.setattr(postgres_chat_agent, "_verify_metric_pin", pin_is_current_only_for_precheck)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="Show net revenue",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
        )
    ]

    assert pin_checks == 3  # chat precheck, gateway closure, post-result check
    assert ChatStreamEventType.POSTGRES_RESULT not in [event.event for event in events]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "metric_pin_invalid"


@pytest.mark.asyncio
async def test_metric_change_after_gateway_withholds_rows_before_chat_delivery(
    monkeypatch, settings_with_key
):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(asset_id=asset_id, projections=({"column_id": column_id},), limit=5)
    metric = SimpleNamespace(
        metric_id=uuid.uuid4(),
        metric_version_id=uuid.uuid4(),
        version_number=3,
        snapshot_id=snapshot_id,
    )
    pin_checks = 0
    seen_executor_kwargs = []

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan",
            message="ready",
            snapshot_id=snapshot_id,
            plan=plan,
            metric_definition=metric,
        )

    async def pin_is_current_then_invalid(**_kwargs):
        nonlocal pin_checks
        pin_checks += 1
        return pin_checks == 1

    async def fake_executor(*_args, **kwargs):
        seen_executor_kwargs.append(kwargs)
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    monkeypatch.setattr(postgres_chat_agent, "_verify_metric_pin", pin_is_current_then_invalid)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="Show net revenue",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
        )
    ]

    assert pin_checks == 2
    assert seen_executor_kwargs[0]["metric_definition_version_id"] == metric.metric_version_id
    assert ChatStreamEventType.POSTGRES_RESULT not in [event.event for event in events]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "metric_pin_invalid"
    assert "No result rows were shown" in saved[-1].content


@pytest.mark.asyncio
async def test_confirmed_metric_result_retains_safe_provenance(monkeypatch, settings_with_key):
    owner_id, source_id, snapshot_id, asset_id, column_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    plan = PostgresQueryPlan(asset_id=asset_id, projections=({"column_id": column_id},), limit=5)
    metric = SimpleNamespace(
        metric_id=uuid.uuid4(),
        metric_version_id=uuid.uuid4(),
        version_number=2,
        snapshot_id=snapshot_id,
    )
    provenance = PostgresMetricProvenance(
        metric_id=metric.metric_id,
        version_number=2,
        name="Net revenue",
        aggregation="sum",
        aggregation_column_name="amount",
        unit_label="USD",
        default_date_column_name="booked_at",
        default_time_zone="UTC",
        fixed_filter_count=1,
    )

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan",
            message="ready",
            snapshot_id=snapshot_id,
            plan=plan,
            metric_definition=metric,
        )

    async def pin_is_current(**_kwargs):
        return True

    async def fake_executor(*_args, **_kwargs):
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    monkeypatch.setattr(postgres_chat_agent, "_verify_metric_pin", pin_is_current)
    monkeypatch.setattr(postgres_chat_agent.metric_service, "metric_provenance", lambda _resolved: provenance)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="Show net revenue",
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
    assert result_event.data["postgres_result"]["metric_provenance"]["metric_id"] == str(metric.metric_id)
    assert result_event.data["postgres_result"]["metric_provenance"]["version_number"] == 2
    assert saved[-1].postgres_result is not None
    assert saved[-1].postgres_result.metric_provenance == provenance


@pytest.mark.asyncio
async def test_approved_relationship_is_pinned_for_gateway_and_result_delivery(
    monkeypatch, settings_with_key
):
    owner_id, source_id, snapshot_id, asset_id, lookup_asset_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    relationship = SimpleNamespace(
        relationship_id=uuid.uuid4(),
        relationship_version_id=uuid.uuid4(),
        version_number=7,
        snapshot_id=snapshot_id,
        join_type="left",
    )
    plan = PostgresQueryPlan(
        asset_id=asset_id,
        join={
            "relationship_id": relationship.relationship_id,
            "relationship_version_id": relationship.relationship_version_id,
            "lookup_asset_id": lookup_asset_id,
            "join_type": "left",
        },
        aggregations=({"operation": "count_all"},),
        limit=5,
    )
    checks = 0

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan",
            message="ready",
            snapshot_id=snapshot_id,
            plan=plan,
            relationship_definition=relationship,
        )

    async def relationship_is_current(**_kwargs):
        nonlocal checks
        checks += 1
        return True

    async def fake_executor(*_args, **kwargs):
        assert kwargs["relationship_definition_version_id"] == relationship.relationship_version_id
        assert await kwargs["relationship_pin_verifier"]() is True
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    monkeypatch.setattr(postgres_chat_agent, "_verify_relationship_pin", relationship_is_current)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="Show revenue by customer region",
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
    provenance = result_event.data["postgres_result"]["relationship_provenance"]
    assert provenance == {
        "relationship_id": str(relationship.relationship_id),
        "relationship_version_id": str(relationship.relationship_version_id),
        "version_number": 7,
        "join_type": "left",
    }
    assert checks >= 4  # chat precheck, gateway closure, post-remote, delivery guard
    assert saved[-1].postgres_result is not None
    assert saved[-1].postgres_result.relationship_provenance == PostgresRelationshipProvenance(
        relationship_id=relationship.relationship_id,
        relationship_version_id=relationship.relationship_version_id,
        version_number=7,
        join_type="left",
    )


@pytest.mark.asyncio
async def test_changed_relationship_pin_blocks_execution_before_gateway(monkeypatch, settings_with_key):
    owner_id, source_id, snapshot_id, asset_id, lookup_asset_id = [uuid.uuid4() for _ in range(5)]
    session = ConversationSession(session_id=uuid.uuid4(), source_id=source_id)
    source = SimpleNamespace(id=source_id, owner_user_id=owner_id)
    saved = await _install_manager(monkeypatch, session=session, source=source)
    relationship = SimpleNamespace(
        relationship_id=uuid.uuid4(),
        relationship_version_id=uuid.uuid4(),
        version_number=1,
        snapshot_id=snapshot_id,
        join_type="inner",
    )
    plan = PostgresQueryPlan(
        asset_id=asset_id,
        join={
            "relationship_id": relationship.relationship_id,
            "relationship_version_id": relationship.relationship_version_id,
            "lookup_asset_id": lookup_asset_id,
            "join_type": "inner",
        },
        aggregations=({"operation": "count_all"},),
        limit=5,
    )
    executor_called = False

    async def fake_plan(**_kwargs):
        return PostgresPlanningResult(
            kind="plan",
            message="ready",
            snapshot_id=snapshot_id,
            plan=plan,
            relationship_definition=relationship,
        )

    async def relationship_is_invalid(**_kwargs):
        return False

    async def fake_executor(*_args, **_kwargs):
        nonlocal executor_called
        executor_called = True
        return _execution(source_id=source_id, snapshot_id=snapshot_id)

    @asynccontextmanager
    async def factory():
        yield _Session()

    monkeypatch.setattr(postgres_chat_agent, "plan_postgres_question", fake_plan)
    monkeypatch.setattr(postgres_chat_agent, "_verify_relationship_pin", relationship_is_invalid)
    events = [
        event
        async for event in postgres_chat_agent.stream_postgres_chat_turn(
            session_id=session.session_id,
            user_message="Show revenue by customer region",
            session_db=_Session(),
            owner_user_id=owner_id,
            settings=settings_with_key,
            authorized_session=session,
            authorized_source=source,
            executor=fake_executor,
            session_factory=factory,
        )
    ]

    assert executor_called is False
    assert ChatStreamEventType.EXECUTING not in [event.event for event in events]
    assert ChatStreamEventType.POSTGRES_RESULT not in [event.event for event in events]
    assert saved[-1].postgres_result is None
    assert saved[-1].degraded_reason == "relationship_pin_invalid"
