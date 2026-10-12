"""Drive ``stream_postgres_chat_turn`` end to end with a scripted model and a scripted executor (SPEC-09 L1-3..5)."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.db.models.catalog import CatalogSnapshot
from app.services.postgres_query_executor import PostgresQueryDeliveryBinding
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational import postgres_chat_agent as agent
from agentic_engine.conversational.models import (
    ChatMessage,
    ConversationMode,
    ConversationSession,
    PostgresChatResult,
)
from tests.nl_sql.conftest import StubLLM
from tests.nl_sql.helpers import ScriptedExecutor, ok_result


class _Db:
    async def rollback(self):
        return None


@dataclass
class Run:
    events: list
    saved: list[ChatMessage]
    llm: Any
    executor: ScriptedExecutor
    cancellation: PostgresExecutionCancellation
    appended: list[dict] = field(default_factory=list)
    guard_calls: int = 0

    @property
    def types(self) -> list[str]:
        return [e.event.value for e in self.events]

    def data(self, name: str) -> list[dict]:
        return [e.data for e in self.events if e.event.value == name]

    @property
    def assistant(self) -> ChatMessage:
        return [m for m in self.saved if m.role.value == "ASSISTANT"][-1]


async def head_snapshot(world) -> uuid.UUID:
    async with world.factory() as session:
        return (await session.execute(select(CatalogSnapshot.id).order_by(CatalogSnapshot.created_at.desc()))).scalars().first()


async def drive(
    world,
    monkeypatch,
    *,
    question: str,
    responses: list,
    script: list | None = None,
    settings=None,
    prior: list[ChatMessage] | None = None,
    metric_match: Any = None,
    plan: Any = None,
    cancellation: PostgresExecutionCancellation | None = None,
    on_guard: Any = None,
    authorize: bool = True,
    llm: Any = "default",
    revoke_after_first_guard: bool = False,
) -> Run:
    snapshot_id = await head_snapshot(world)
    settings = settings or world.settings
    session_id = uuid.uuid4()
    saved: list[ChatMessage] = list(prior or [])
    appended: list[dict] = []
    cancellation = cancellation or PostgresExecutionCancellation()

    def fresh_result(sql="SELECT count(*) AS n FROM sales.orders", n=42):
        return ok_result(sql, n, source_id=world.source_id, snapshot_id=snapshot_id)

    executor = ScriptedExecutor(script if script is not None else [fresh_result()])

    async def append_message(*, session_id, role, content, postgres_result=None, **kwargs):
        appended.append({"role": role, "content": content, **kwargs, "postgres_result": postgres_result})
        message = ChatMessage(
            message_id=uuid.uuid4(), session_id=session_id, role=role, content=content,
            created_at=datetime.now(timezone.utc), mode_used=kwargs.get("mode_used"),
            postgres_result=(None if postgres_result is None else PostgresChatResult.model_validate(postgres_result.model_dump(mode="python"))),
            nl_sql=kwargs.get("nl_sql"), degraded=kwargs.get("degraded", False),
            degraded_reason=kwargs.get("degraded_reason"), unsupported=kwargs.get("unsupported", False),
            token_usage=kwargs.get("token_usage"),
        )
        saved.append(message)
        return message

    async def recent(*_a, **_k):
        return list(saved)

    session = ConversationSession(session_id=session_id, source_id=world.source_id, mode=ConversationMode.AUTO)
    source = SimpleNamespace(id=world.source_id, owner_user_id=world.owner_id)

    async def authorized(*_a, **_k):
        return manager.AuthorizedConversation(session=session, source=source)

    run = Run(events=[], saved=saved, llm=None, executor=executor, cancellation=cancellation, appended=appended)

    @asynccontextmanager
    async def guard(*_a, **_k):
        run.guard_calls += 1
        if on_guard is not None:
            await on_guard(run, run.guard_calls)
        if revoke_after_first_guard and run.guard_calls >= 1:
            raise RuntimeError("revoked")
        yield _Db()

    def binding(result, *, owner_user_id, source_id):
        return PostgresQueryDeliveryBinding(
            owner_user_id=owner_user_id, source_id=source_id, snapshot_id=result.snapshot_id,
            configuration_generation=1, required_relations=(("sales", "orders"),),
        )

    async def match(_session, **_k):
        return metric_match

    async def authorize_ok(**_k):
        return authorize

    monkeypatch.setattr(agent.manager, "append_message", append_message)
    monkeypatch.setattr(agent.manager, "get_recent_messages", recent)
    monkeypatch.setattr(agent.manager, "get_session_for_access", authorized)
    monkeypatch.setattr(agent, "_delivery_binding_for_result", binding)
    monkeypatch.setattr(agent, "postgres_query_delivery_guard", guard)
    monkeypatch.setattr(agent, "_authorize_before_disclosure", authorize_ok)
    monkeypatch.setattr(agent.metric_service, "match_metric_definition_for_question", match)
    if plan is not None:
        monkeypatch.setattr(agent, "plan_postgres_question", plan)
    stub = StubLLM(responses=responses) if llm == "default" else llm
    run.llm = stub

    async for event in agent.stream_postgres_chat_turn(
        session_id=session_id, user_message=question, session_db=_Db(), owner_user_id=world.owner_id,
        settings=settings, llm=stub, cancellation=cancellation, authorized_session=session, authorized_source=source,
        session_factory=world.factory, sql_execute=executor,
    ):
        run.events.append(event)
    return run
