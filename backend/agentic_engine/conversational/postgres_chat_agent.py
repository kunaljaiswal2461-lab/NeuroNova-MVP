"""Stage 4C source-bound PostgreSQL chat turn orchestration.

It reuses the conversation session/message model and the existing SSE wire
format, but deliberately never enters the uploaded-file RAG/Polars paths.
Planning is structured and bounded; all database execution goes through the
Stage 4B gateway.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.services.postgres_query_executor import (
    PostgresQueryExecutionResult,
    execute_postgres_query,
)
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.models import (
    ChatMessage,
    ChatRole,
    ChatStreamEvent,
    ChatStreamEventType,
    ConversationMode,
    ConversationSession,
)
from agentic_engine.conversational.postgres_planner import (
    PostgresPlanningResult,
    plan_postgres_question,
    prior_postgres_asset_id,
    postgres_history_for_prompt,
)
from agentic_engine.llm_engine.base_llm import LLMClient
from agentic_engine.llm_engine.openai_client import build_client


def _event(event: ChatStreamEventType, data: dict[str, Any]) -> ChatStreamEvent:
    return ChatStreamEvent(event=event, data=data)


def _summary_for_result(result: PostgresQueryExecutionResult) -> str:
    if result.status == "cancelled":
        return "The database calculation was cancelled. No result rows were used."
    if result.status != "succeeded":
        return (
            "The database calculation did not complete, so I cannot provide an "
            "answer from database rows."
        )
    if result.returned_row_count == 0:
        return "The read-only calculation completed successfully; no rows matched."
    completeness = ""
    if not result.complete:
        completeness = " The displayed result is bounded and may be truncated."
    return (
        f"The read-only calculation completed with {result.returned_row_count} returned row(s)."
        f"{completeness}"
    )


async def _persist_assistant(
    *,
    session_db: AsyncSession,
    session_id: uuid.UUID,
    content: str,
    postgres_result: PostgresQueryExecutionResult | None = None,
    unsupported: bool = False,
    degraded: bool = False,
    degraded_reason: str | None = None,
    token_usage: Any | None = None,
) -> ChatMessage:
    return await manager.append_message(
        session_db=session_db,
        session_id=session_id,
        role=ChatRole.ASSISTANT,
        content=content,
        mode_used=ConversationMode.QUERY,
        postgres_result=postgres_result,
        unsupported=unsupported,
        degraded=degraded,
        degraded_reason=degraded_reason,
        token_usage=token_usage,
        commit=True,
    )


def _done(message: ChatMessage, *, token_usage: Any | None = None) -> ChatStreamEvent:
    payload: dict[str, Any] = {
        "message_id": str(message.message_id),
        "session_id": str(message.session_id),
        "degraded": message.degraded,
        "degraded_reason": message.degraded_reason,
        "unsupported": message.unsupported,
    }
    if token_usage is not None:
        payload["token_usage"] = token_usage.model_dump(mode="json")
    return _event(ChatStreamEventType.DONE, payload)


async def _authorize_before_disclosure(
    *,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Do not disclose a completed result after its source/session vanished."""

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            conversation = await manager.get_session_for_access(
                fresh_session,
                session_id,
                access=manager.ConversationAccess.for_user(owner_user_id),
            )
            return (
                conversation.session.source_id is not None
                and conversation.source is not None
                and conversation.source.id == conversation.session.source_id
            )
    except Exception:
        # A missing/deleted source is intentionally indistinguishable here;
        # any result rows must remain undisclosed.
        return False


async def _persist_cancelled_after_disconnect(
    *,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> None:
    """Best-effort terminal persistence using a fresh app DB session.

    The caller's request session may itself be cancelled.  This does not
    resurrect a deleted source: manager authorization is repeated first.
    """

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            await manager.get_session_for_access(
                fresh_session,
                session_id,
                access=manager.ConversationAccess.for_user(owner_user_id),
            )
            await _persist_assistant(
                session_db=fresh_session,
                session_id=session_id,
                content="The database calculation was cancelled. No result rows were used.",
                degraded=True,
                degraded_reason="execution_cancelled",
            )
    except Exception:
        # Disconnect cleanup must never leak a driver/detail string or keep a
        # cancelled ASGI task alive.  The user turn already records intent.
        return


async def stream_postgres_chat_turn(
    *,
    session_id: uuid.UUID,
    user_message: str,
    session_db: AsyncSession,
    owner_user_id: uuid.UUID,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    cancellation: PostgresExecutionCancellation | None = None,
    authorized_session: ConversationSession | None = None,
    authorized_source: Any | None = None,
    executor: Callable[..., Any] | None = None,
    session_factory: Callable[[], Any] | None = None,
    turn_id: uuid.UUID | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Run one source-bound chat turn without ever accepting raw SQL.

    User messages are committed before planning because the remote gateway uses
    its own short-lived session factory.  Every normal terminal path writes
    exactly one assistant message; cancellation is separately best-effort so
    an HTTP disconnect cannot strand a mutable request transaction.
    """

    settings = settings or get_settings()
    app_session_factory = session_factory or get_sessionmaker()
    cancellation = cancellation or PostgresExecutionCancellation()
    turn_id = turn_id or uuid.uuid4()
    # A user turn is committed before planning.  Track the terminal assistant
    # separately so an SSE disconnect/``aclose`` can add one safe cancellation
    # record without duplicating a completed normal turn.
    terminal_persisted = False
    cleanup_started = False
    if (authorized_session is None) != (authorized_source is None):
        raise ValueError("authorized source conversation context must be supplied together")
    if authorized_session is None:
        conversation = await manager.get_session_for_access(
            session_db,
            session_id,
            access=manager.ConversationAccess.for_user(owner_user_id),
        )
        session = conversation.session
        source = conversation.source
    else:
        session = authorized_session
        source = authorized_source
    if (
        session.session_id != session_id
        or session.source_id is None
        or source is None
        or getattr(source, "id", None) != session.source_id
        or getattr(source, "owner_user_id", None) != owner_user_id
    ):
        raise ValueError("authorized database conversation context does not match session")

    user_message_row = await manager.append_message(
        session_db=session_db,
        session_id=session_id,
        role=ChatRole.USER,
        content=user_message,
        commit=True,
    )
    async def _cancelled_events() -> tuple[ChatStreamEvent, ...]:
        """Persist a cancellation outcome without ever retaining result rows."""

        nonlocal terminal_persisted
        try:
            # The user message was committed above.  Discard any planner read
            # transaction before adding its one matching terminal assistant.
            await session_db.rollback()
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content="The database calculation was cancelled. No result rows were used.",
                degraded=True,
                degraded_reason="execution_cancelled",
            )
            terminal_persisted = True
            return (
                _event(
                    ChatStreamEventType.TOKEN,
                    {"text": "The database calculation was cancelled. No result rows were used."},
                ),
                _done(assistant),
            )
        except Exception:
            # A deleted source/session cannot be recreated just to write a
            # cancellation record.  The outer cleanup still repeats the
            # authorization in a fresh app session as a best effort.
            return (_event(ChatStreamEventType.DONE, {"session_id": str(session_id)}),)

    async def _cleanup_interrupted_turn() -> None:
        """Stop remote work and repair a user turn closed at an SSE yield."""

        nonlocal cleanup_started
        if terminal_persisted or cleanup_started:
            return
        cleanup_started = True
        try:
            await asyncio.shield(cancellation.cancel())
            await asyncio.shield(
                _persist_cancelled_after_disconnect(
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    session_factory=app_session_factory,
                )
            )
        except Exception:
            # Cancellation cleanup cannot disclose a driver or persistence
            # detail, and source deletion makes this a harmless no-op.
            return

    try:
        yield _event(
            ChatStreamEventType.START,
            {
                "session_id": str(session_id),
                "user_message_id": str(user_message_row.message_id),
                "turn_id": str(turn_id),
                "binding": "postgres_source",
            },
        )
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        yield _event(ChatStreamEventType.PLANNING, {"state": "planning"})
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        # The planner needs only safe transcript prose and catalog metadata.
        # It cannot receive retained typed rows from a prior database result.
        messages = await manager.get_recent_messages(session_db, session_id)
        history = postgres_history_for_prompt(messages[:-1])
        prior_asset_id = prior_postgres_asset_id(messages[:-1])
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return
        active_llm = llm if llm is not None else build_client(settings.openai_api_key)
        planning = await plan_postgres_question(
            session=session_db,
            owner_user_id=owner_user_id,
            source_id=session.source_id,
            question=user_message,
            history=history,
            settings=settings,
            llm=active_llm,
            prior_asset_id=prior_asset_id,
        )
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        if not planning.executable:
            event_type = {
                "clarification": ChatStreamEventType.CLARIFICATION,
                "unsupported": ChatStreamEventType.UNSUPPORTED,
                "catalog": ChatStreamEventType.CATALOG,
            }.get(planning.kind, ChatStreamEventType.ERROR)
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=planning.message,
                unsupported=planning.kind == "unsupported",
                degraded=planning.kind == "failure",
                degraded_reason="planning_failure" if planning.kind == "failure" else None,
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            # Persist before exposing the terminal outcome. A disconnect after
            # an SSE frame must not leave an accepted user turn unmatched.
            yield _event(
                event_type,
                {
                    "message": planning.message,
                    "outcome": planning.kind,
                    "snapshot_id": str(planning.snapshot_id) if planning.snapshot_id else None,
                },
            )
            yield _event(ChatStreamEventType.TOKEN, {"text": planning.message})
            yield _done(assistant, token_usage=planning.token_usage)
            return

        # The request session was used only for transcript/catalog reads. End
        # its read transaction before remote I/O; Stage 4B opens its own
        # bounded app sessions and revalidates all authorization itself.
        await session_db.rollback()
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return
        yield _event(
            ChatStreamEventType.EXECUTING,
            {
                "state": "executing",
                "source_id": str(session.source_id),
                "snapshot_id": str(planning.snapshot_id),
            },
        )
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        execute = executor or execute_postgres_query
        try:
            result = await execute(
                app_session_factory,
                owner_user_id=owner_user_id,
                source_id=session.source_id,
                snapshot_id=planning.snapshot_id,
                plan=planning.plan,
                settings=settings,
                cancellation=cancellation,
            )
        except Exception:
            # Stage 4B returns safe typed failures for expected problems. This
            # guard covers only unexpected orchestration failures and must
            # never invent a result or echo an exception.
            if cancellation.cancelled:
                for event in await _cancelled_events():
                    yield event
                return
            message = "The database calculation could not be started safely, so no answer was generated."
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=message,
                degraded=True,
                degraded_reason="execution_failed",
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            yield _done(assistant, token_usage=planning.token_usage)
            return

        # Explicit cancellation can race the executor's successful return.
        # Treat a winning cancellation as terminal before any authorization,
        # persistence, or SSE path can inspect the returned rows.
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        if not await _authorize_before_disclosure(
            owner_user_id=owner_user_id,
            session_id=session_id,
            session_factory=app_session_factory,
        ):
            # A source deletion/revocation after execution is never a licence
            # to release a buffered result. Do not recreate that session.
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "Access changed before the database result could be delivered."},
            )
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
            return
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        summary = _summary_for_result(result)
        # Atomically order a cancel request and the final row-bearing write.
        # A cancellation already accepted means we never retain the result;
        # one accepted later is still checked before SSE disclosure below.
        delivery_allowed = False
        async with cancellation.delivery_guard() as delivery_allowed:
            if delivery_allowed:
                assistant = await _persist_assistant(
                    session_db=session_db,
                    session_id=session_id,
                    content=summary,
                    postgres_result=result,
                    degraded=not result.is_ok,
                    degraded_reason=result.failure_code,
                    token_usage=planning.token_usage,
                )
                terminal_persisted = True
        if not delivery_allowed:
            for event in await _cancelled_events():
                yield event
            return

        # Store the bounded typed result before sending it, then repeat owner
        # resolution after the write. These checks close executor-to-write and
        # write-to-disclosure gaps without treating a session ID as authority.
        if cancellation.cancelled:
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "The database calculation was cancelled before its result could be delivered."},
            )
            yield _done(assistant, token_usage=planning.token_usage)
            return
        if not await _authorize_before_disclosure(
            owner_user_id=owner_user_id,
            session_id=session_id,
            session_factory=app_session_factory,
        ):
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "Access changed before the database result could be delivered."},
            )
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
            return
        if cancellation.cancelled:
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "The database calculation was cancelled before its result could be delivered."},
            )
            yield _done(assistant, token_usage=planning.token_usage)
            return
        safe_result = result.model_dump(mode="json")
        yield _event(ChatStreamEventType.POSTGRES_RESULT, {"postgres_result": safe_result})
        yield _event(ChatStreamEventType.TOKEN, {"text": summary})
        yield _done(assistant, token_usage=planning.token_usage)
    except asyncio.CancelledError:
        await _cleanup_interrupted_turn()
        raise
    finally:
        # Routes may close an async generator directly after an SSE frame,
        # which runs ``finally`` without raising CancelledError at the inner
        # executor await. This repairs a committed user turn in that path too.
        await _cleanup_interrupted_turn()


__all__ = ["stream_postgres_chat_turn"]
