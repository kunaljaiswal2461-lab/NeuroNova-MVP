"""Stage 4C source-bound PostgreSQL chat turn orchestration.

It reuses the conversation session/message model and the existing SSE wire
format, but deliberately never enters the uploaded-file RAG/Polars paths.
Planning is structured and bounded; all database execution goes through the
Stage 4B gateway.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.schemas.nl_sql_record import NlSqlTurn
from app.services import column_privacy, metric_service, relationship_service
from app.services.nl_sql import messages as nl_messages
from app.services.nl_sql.adapter import to_chat_result
from app.services.nl_sql.history import history_from_messages
from app.services.nl_sql.cancel import TurnCancelled
from app.services.nl_sql.pass2 import write_presentation
from app.services.nl_sql.pipeline import PipelineEvent, PipelineOutcome, RepairRecord, answer_question
from app.services.nl_sql.turn_record import build_turn_record, presentation_record, sse_presentation
from app.services.nl_sql.write_guard import is_write_or_ddl_request
from app.services.postgres_query_executor import (
    PostgresQueryDeliveryBinding,
    PostgresQueryExecutionResult,
    PostgresRelationshipProvenance,
    execute_postgres_query,
    postgres_query_delivery_guard,
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
from agentic_engine.llm_engine.factory import build_llm, llm_available
from agentic_engine.llm_engine.models import TokenUsage
from agentic_engine.llm_engine.openai_client import build_client


def _event(event: ChatStreamEventType, data: dict[str, Any]) -> ChatStreamEvent:
    return ChatStreamEvent(event=event, data=data)


class _PostgresResultPersistenceError(Exception):
    """A local transcript write failed after a delivery fence was accepted."""


def _summary_for_result(result: PostgresQueryExecutionResult) -> str:
    if result.status == "cancelled":
        return "The database calculation was cancelled. No result rows were used."
    if result.status != "succeeded":
        return (
            "The database calculation did not complete, so I cannot provide an "
            "answer from database rows."
        )
    # A byte cap can reject the very first serialized row.  That is a
    # successful, deliberately bounded calculation, not evidence that no
    # database rows matched the plan.
    if not result.complete and result.returned_row_count == 0:
        return (
            "The read-only calculation completed, but no result rows could be safely "
            "displayed before a safety limit was reached. The result is incomplete."
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
    commit: bool = True,
    nl_sql: NlSqlTurn | None = None,
) -> ChatMessage:
    # ``nl_sql`` is passed only by the model-written-SQL path, so a typed-path turn calls the manager exactly as before.
    extra: dict[str, Any] = {"nl_sql": nl_sql} if nl_sql is not None else {}
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
        commit=commit,
        **extra,
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


def _delivery_binding_for_result(
    result: PostgresQueryExecutionResult,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
) -> PostgresQueryDeliveryBinding | None:
    """Accept only the executor's non-serializable source delivery fence."""

    binding = result.delivery_binding
    if (
        binding is None
        or binding.owner_user_id != owner_user_id
        or binding.source_id != source_id
        or result.source_id != source_id
        or result.snapshot_id != binding.snapshot_id
    ):
        return None
    return binding


async def _persist_postgres_result_with_delivery_guard(
    *,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID,
    source_id: uuid.UUID,
    result: PostgresQueryExecutionResult,
    content: str,
    settings: Settings,
    session_factory: Callable[[], Any],
    degraded: bool,
    degraded_reason: str | None,
    token_usage: Any | None,
    nl_sql: NlSqlTurn | None = None,
) -> ChatMessage | None:
    """Commit one row-bearing message while the saved source remains fenced.

    Scope and credential maintenance take the same source-row lock.  A change
    that committed before this point invalidates the executor binding; a change
    that waits behind this commit observes and clears this newly persisted
    result before it can become a retained stale transcript blob.
    """

    binding = _delivery_binding_for_result(
        result, owner_user_id=owner_user_id, source_id=source_id
    )
    if binding is None:
        return None
    try:
        async with postgres_query_delivery_guard(
            session_factory,
            owner_user_id=owner_user_id,
            binding=binding,
            settings=settings,
        ) as delivery_session:
            conversation = await manager.get_session_for_access(
                delivery_session,
                session_id,
                access=manager.ConversationAccess.for_user(owner_user_id),
            )
            if (
                conversation.session.source_id != source_id
                or conversation.source is None
                or conversation.source.id != source_id
            ):
                return None
            try:
                return await _persist_assistant(
                    session_db=delivery_session,
                    session_id=session_id,
                    content=content,
                    postgres_result=result,
                    degraded=degraded,
                    degraded_reason=degraded_reason,
                    token_usage=token_usage,
                    commit=False,
                    nl_sql=nl_sql,
                )
            except Exception as exc:
                raise _PostgresResultPersistenceError() from exc
    except asyncio.CancelledError:
        raise
    except _PostgresResultPersistenceError:
        raise
    except Exception:
        # The caller emits only a generic access/delivery failure.  Never turn
        # a failed fence into a fallback path that retains or streams rows.
        return None


async def _verify_metric_pin(
    *,
    metric_definition: Any,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Fail closed if a confirmed metric changed around execution or disclosure.

    The metric resolver pins an immutable version plus the current catalog
    snapshot during planning.  This fresh-session check prevents a definition
    edit, retirement, scope change, or catalog change from turning an already
    prepared generic plan into an executable authority or disclosable result.
    """

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            return bool(
                await metric_service.verify_metric_pin_for_execution(
                    fresh_session,
                    owner_user_id=owner_user_id,
                    source_id=source_id,
                    metric_id=metric_definition.metric_id,
                    version_number=metric_definition.version_number,
                    snapshot_id=metric_definition.snapshot_id,
                    metric_version_id=getattr(metric_definition, "metric_version_id", None),
                )
            )
    except Exception:
        # A lifecycle/persistence/access race is never a reason to execute a
        # stale definition.  The terminal response below intentionally omits
        # backend detail and does not expose rows.
        return False


async def _verify_relationship_pin(
    *,
    relationship_definition: Any,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Fail closed if an approved lookup changed around result delivery."""

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            return bool(
                await relationship_service.verify_relationship_pin_for_execution(
                    fresh_session,
                    owner_user_id=owner_user_id,
                    source_id=source_id,
                    relationship_id=relationship_definition.relationship_id,
                    version_number=relationship_definition.version_number,
                    snapshot_id=relationship_definition.snapshot_id,
                    relationship_version_id=getattr(
                        relationship_definition, "relationship_version_id", None
                    ),
                )
            )
    except Exception:
        # The resolver hides lifecycle, catalog, and persistence details. A
        # failed recheck is never a reason to execute or disclose rows.
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


# ── SPEC-09: the model-written-SQL path ──────────────────────────────────────────────────────────

def _sql_sse(event: PipelineEvent, *, source_id: uuid.UUID) -> ChatStreamEvent:
    if event.name == "sql_generated":
        return _event(ChatStreamEventType.SQL_GENERATED, dict(event.data))
    if event.name == "repairing":
        return _event(ChatStreamEventType.REPAIRING, {"reason_code": event.data.get("reason_code")})
    return _event(
        ChatStreamEventType.EXECUTING,
        {"state": "executing", "source_id": str(source_id), "snapshot_id": event.data.get("snapshot_id")},
    )


async def _question_names_a_metric(
    session_db: AsyncSession, *, owner_user_id: uuid.UUID, source_id: uuid.UUID, question: str
) -> bool:
    """R3: does the question name a confirmed metric (matched or ambiguous)? ``unavailable`` and errors mean no."""
    try:
        match = await metric_service.match_metric_definition_for_question(
            session_db, owner_user_id=owner_user_id, source_id=source_id, question=question
        )
    except Exception:
        match = None
    finally:
        try:
            await session_db.rollback()
        except Exception:
            pass
    return match is not None and getattr(match, "kind", None) in ("matched", "ambiguous")


def _usage_of(calls: Any, extra: TokenUsage | None = None) -> TokenUsage:
    usage = TokenUsage()
    if extra is not None:
        usage.input_tokens, usage.output_tokens, usage.call_count = (
            extra.input_tokens, extra.output_tokens, extra.call_count
        )
    for call in calls:
        usage.add(call.input_tokens, call.output_tokens)
    return usage


async def _load_privacy_flags(session_factory: Callable[[], Any], source_id: uuid.UUID):
    """Effective personal-data flags of the head snapshot, read once. ``None`` means unreadable (fail closed)."""
    try:
        async with session_factory() as session:
            try:
                return await column_privacy.load_effective_flags(session, source_id)
            finally:
                await session.rollback()
    except Exception:
        return None


async def _stream_sql_path(
    *,
    route: str,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    user_message: str,
    session_db: AsyncSession,
    settings: Settings,
    llm: LLMClient | None,
    cancellation: PostgresExecutionCancellation,
    app_session_factory: Callable[[], Any],
    messages: list[ChatMessage],
    carried_usage: TokenUsage | None,
    set_persisted: Callable[[], None],
    set_emitted: Callable[[], None],
    cancelled_events: Callable[[], Any],
    sql_execute: Callable[..., Any] | None = None,
    sql_execute_kwargs: Mapping[str, Any] | None = None,
    turn_started: datetime | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Pass 1 (+ at most one repair), execution, pass 2, and the guarded delivery of one answer."""

    turn_started = turn_started or datetime.now(timezone.utc)
    if llm is None and not llm_available(settings):
        # The typed path is unaffected; only a SQL-path turn needs the model (W2.1).
        message = nl_messages.MODEL_NOT_CONFIGURED
        assistant = await _persist_assistant(
            session_db=session_db, session_id=session_id, content=message, degraded=True,
            degraded_reason="model_unavailable", token_usage=carried_usage,
        )
        set_persisted()
        yield _event(ChatStreamEventType.ERROR, {"code": "model_unavailable", "message": message})
        yield _event(ChatStreamEventType.TOKEN, {"text": message})
        yield _done(assistant, token_usage=carried_usage)
        return
    active_llm = llm if llm is not None else build_llm(settings)
    history = history_from_messages(messages[:-1])

    queue: asyncio.Queue[PipelineEvent] = asyncio.Queue()

    async def on_event(event: PipelineEvent) -> None:
        await queue.put(event)

    task = asyncio.ensure_future(
        answer_question(
            session_factory=app_session_factory,
            settings=settings,
            llm=active_llm,
            user_id=owner_user_id,
            source_id=source_id,
            question=user_message,
            history=history,
            reference_time=turn_started,
            time_zone=settings.nl_sql_default_time_zone,
            cancellation=cancellation,
            on_event=on_event,
            execute=sql_execute,
            execute_kwargs=sql_execute_kwargs,
        )
    )
    budget = settings.nl_sql_turn_budget_seconds
    budget_expired = False
    first_sql: str | None = None
    last_sql: str | None = None
    repair_reason: str | None = None

    def remaining_budget() -> float:
        return budget - (datetime.now(timezone.utc) - turn_started).total_seconds()

    def note_event(event: PipelineEvent) -> None:
        nonlocal first_sql, last_sql, repair_reason
        if event.name == "sql_generated" and isinstance(event.data.get("sql"), str):
            first_sql = first_sql or event.data["sql"]
            last_sql = event.data["sql"]
        elif event.name == "repairing":
            repair_reason = str(event.data.get("reason_code") or "") or None

    outcome: PipelineOutcome | None = None
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, task}, timeout=max(remaining_budget(), 0), return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                pipeline_event = getter.result()
                note_event(pipeline_event)
                yield _sql_sse(pipeline_event, source_id=source_id)
                continue
            getter.cancel()
            if not done:
                # SPEC-10 W1.2: the turn budget ran out. Flag it first so it is never reported as a user cancel.
                budget_expired = True
            break
        if not budget_expired:
            while not queue.empty():
                yield _sql_sse(queue.get_nowait(), source_id=source_id)
            outcome = task.result()
    finally:
        if not task.done():
            task.cancel()

    async def budget_expired_events() -> list[ChatStreamEvent]:
        """Fail the turn with ``turn_timeout``; the turn's connection and model tasks are cancelled as on a user cancel."""
        await cancellation.cancel()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await session_db.rollback()
        text = nl_messages.TURN_TIMEOUT
        timed_out = PipelineOutcome(
            kind="failed", sql=last_sql, failure_code="turn_timeout", user_message=text,
            repair=RepairRecord(reason_code=repair_reason, original_sql=first_sql)
            if repair_reason and first_sql and first_sql != last_sql else None,
        )
        record = build_turn_record(route=route, outcome=timed_out, presentation=None, llm_calls=(), pass2_ran=False)
        assistant = await _persist_assistant(
            session_db=session_db, session_id=session_id, content=text, degraded=True,
            degraded_reason="turn_timeout", token_usage=carried_usage, nl_sql=record,
        )
        set_persisted()
        return [
            _event(ChatStreamEventType.ERROR, {"code": "turn_timeout", "message": text}),
            _event(ChatStreamEventType.TOKEN, {"text": text}),
            _done(assistant, token_usage=carried_usage),
        ]

    if budget_expired:
        for event in await budget_expired_events():
            yield event
        return
    assert outcome is not None

    if outcome.failure_code == "execution_cancelled" or cancellation.cancelled:
        for event in await cancelled_events():
            yield event
        return
    await session_db.rollback()

    usage = _usage_of(outcome.llm_calls, carried_usage)
    snapshot_id = str(outcome.snapshot_id) if outcome.snapshot_id else None

    if outcome.kind in ("clarification", "unsupported"):
        text = outcome.user_message or nl_messages.GENERIC
        record = build_turn_record(route=route, outcome=outcome, presentation=None, llm_calls=outcome.llm_calls, pass2_ran=False)
        assistant = await _persist_assistant(
            session_db=session_db, session_id=session_id, content=text,
            unsupported=outcome.kind == "unsupported", token_usage=usage, nl_sql=record,
        )
        set_persisted()
        yield _event(
            ChatStreamEventType.CLARIFICATION if outcome.kind == "clarification" else ChatStreamEventType.UNSUPPORTED,
            {"message": text, "outcome": outcome.kind, "snapshot_id": snapshot_id},
        )
        yield _event(ChatStreamEventType.TOKEN, {"text": text})
        yield _done(assistant, token_usage=usage)
        return

    if outcome.kind == "failed":
        text = outcome.user_message or nl_messages.GENERIC
        record = build_turn_record(route=route, outcome=outcome, presentation=None, llm_calls=outcome.llm_calls, pass2_ran=False)
        assistant = await _persist_assistant(
            session_db=session_db, session_id=session_id, content=text, degraded=True,
            degraded_reason=outcome.failure_code, token_usage=usage, nl_sql=record,
        )
        set_persisted()
        yield _event(ChatStreamEventType.ERROR, {"code": outcome.failure_code, "message": text})
        yield _event(ChatStreamEventType.TOKEN, {"text": text})
        yield _done(assistant, token_usage=usage)
        return

    # ── an answer: the rows exist; decide what may leave the process ───────────────────────────────
    execution = outcome.execution
    assert execution is not None and outcome.envelope is not None
    adapted = to_chat_result(
        execution, started_at=turn_started, completed_at=datetime.now(timezone.utc),
        row_cap=settings.postgres_execution_max_result_rows,
    )
    access_changed = {"code": "access_changed", "message": "Access changed before the database result could be delivered."}
    if not await _authorize_before_disclosure(
        owner_user_id=owner_user_id, session_id=session_id, session_factory=app_session_factory
    ):
        set_emitted()
        yield _event(ChatStreamEventType.ERROR, access_changed)
        yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
        return
    binding = _delivery_binding_for_result(adapted, owner_user_id=owner_user_id, source_id=source_id)
    if binding is None:
        set_emitted()
        yield _event(ChatStreamEventType.ERROR, access_changed)
        yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
        return

    # W4.6: a check, not a hold. If the source was revoked since the query ran, no row goes to the model.
    try:
        async with postgres_query_delivery_guard(
            app_session_factory, owner_user_id=owner_user_id, binding=binding, settings=settings
        ):
            pass
        revoked = False
    except asyncio.CancelledError:
        raise
    except Exception:
        revoked = True
    if revoked:
        text = nl_messages.ACCESS_REVOKED
        record = build_turn_record(route=route, outcome=outcome, presentation=None, llm_calls=outcome.llm_calls, pass2_ran=False)
        assistant = await _persist_assistant(
            session_db=session_db, session_id=session_id, content=text, degraded=True,
            degraded_reason="access_revoked", token_usage=usage, nl_sql=record,
        )
        set_persisted()
        yield _event(ChatStreamEventType.ERROR, {"code": "access_revoked", "message": text})
        yield _event(ChatStreamEventType.TOKEN, {"text": text})
        yield _done(assistant, token_usage=usage)
        return
    if cancellation.cancelled:
        for event in await cancelled_events():
            yield event
        return

    consent, _generation = await column_privacy.read_consent(
        app_session_factory, source_id=source_id, owner_user_id=owner_user_id
    )
    flags = await _load_privacy_flags(app_session_factory, source_id) if consent else None
    if consent and flags is None:
        consent = False                                # unreadable privacy flags: send no row
    flags = flags or column_privacy.PrivacyFlags({}, {})
    pass2 = None
    if not cancellation.cancelled:
        yield _event(ChatStreamEventType.PRESENTING, {"state": "presenting"})      # SPEC-10 W2.1
        pass2_task = asyncio.ensure_future(
            write_presentation(
                llm=active_llm, settings=settings, question=user_message, envelope=outcome.envelope,
                execution=execution, flags=flags, consent=consent, repaired=outcome.repair is not None,
                cancellation=cancellation,
            )
        )
        try:
            await asyncio.wait({pass2_task}, timeout=max(remaining_budget(), 0))
            if not pass2_task.done():
                budget_expired = True
        finally:
            if not pass2_task.done():
                pass2_task.cancel()                  # budget expired, or the stream was closed under us
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pass2_task
        if budget_expired:
            for event in await budget_expired_events():
                yield event
            return
        try:
            pass2 = pass2_task.result()
        except TurnCancelled:
            pass2 = None                      # the turn was cancelled during the model call: nothing is delivered
    if pass2 is None or cancellation.cancelled:
        for event in await cancelled_events():
            yield event
        return

    all_calls = (*outcome.llm_calls, *((pass2.llm_call,) if pass2.llm_call is not None else ()))
    usage = _usage_of(all_calls, carried_usage)
    summary = _summary_for_result(adapted)
    presentation = presentation_record(
        pass2, summary=summary, consent=consent, complete=adapted.complete, repaired=outcome.repair is not None
    )
    content = presentation.answer
    record = build_turn_record(
        route=route, outcome=outcome, presentation=presentation, llm_calls=all_calls, pass2_ran=True
    )

    delivery_allowed = False
    assistant: ChatMessage | None = None
    async with cancellation.delivery_guard() as delivery_allowed:
        if delivery_allowed:
            assistant = await _persist_postgres_result_with_delivery_guard(
                owner_user_id=owner_user_id, session_id=session_id, source_id=source_id, result=adapted,
                content=content, settings=settings, session_factory=app_session_factory, degraded=False,
                degraded_reason=None, token_usage=usage, nl_sql=record,
            )
            if assistant is not None:
                set_persisted()
    if not delivery_allowed:
        for event in await cancelled_events():
            yield event
        return
    if assistant is None:
        set_emitted()
        yield _event(ChatStreamEventType.ERROR, access_changed)
        yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
        return
    if cancellation.cancelled or not await _authorize_before_disclosure(
        owner_user_id=owner_user_id, session_id=session_id, session_factory=app_session_factory
    ):
        set_emitted()
        if cancellation.cancelled:
            yield _event(ChatStreamEventType.ERROR, {
                "code": "execution_cancelled",
                "message": "The database calculation was cancelled before its result could be delivered.",
            })
            yield _done(assistant, token_usage=usage)
        else:
            yield _event(ChatStreamEventType.ERROR, access_changed)
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
        return

    sse_allowed = False
    try:
        async with postgres_query_delivery_guard(
            app_session_factory, owner_user_id=owner_user_id, binding=binding, settings=settings
        ) as delivery_session:
            conversation = await manager.get_session_for_access(
                delivery_session, session_id, access=manager.ConversationAccess.for_user(owner_user_id)
            )
            if (
                conversation.session.source_id == source_id
                and conversation.source is not None
                and conversation.source.id == source_id
                and not cancellation.cancelled
            ):
                sse_allowed = True
                set_emitted()
                yield _event(ChatStreamEventType.POSTGRES_RESULT, {"postgres_result": adapted.model_dump(mode="json")})
                yield _event(ChatStreamEventType.PRESENTATION, sse_presentation(presentation))
    except asyncio.CancelledError:
        raise
    except Exception:
        sse_allowed = False
    if not sse_allowed:
        set_emitted()
        if cancellation.cancelled:
            yield _event(ChatStreamEventType.ERROR, {
                "code": "execution_cancelled",
                "message": "The database calculation was cancelled before its result could be delivered.",
            })
            yield _done(assistant, token_usage=usage)
        else:
            yield _event(ChatStreamEventType.ERROR, access_changed)
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
        return
    yield _event(ChatStreamEventType.TOKEN, {"text": content})
    yield _done(assistant, token_usage=usage)


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
    sql_execute: Callable[..., Any] | None = None,
    sql_execute_kwargs: Mapping[str, Any] | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Run one source-bound chat turn. The typed path never accepts raw SQL; the SQL path (hybrid mode) runs model SQL
    only after the validator and the restricted gateway (SPEC-09).

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
    terminal_emitted = False
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

        nonlocal terminal_emitted, terminal_persisted
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
            # The cancellation record is safely committed before an SSE frame
            # is exposed, so direct generator close cannot add a second one.
            terminal_emitted = True
            return (
                _event(
                    ChatStreamEventType.TOKEN,
                    {"text": "The database calculation was cancelled. No result rows were used."},
                ),
                _done(assistant),
            )
        except Exception:
            # A deleted source/session cannot be recreated just to write a
            # cancellation record. Repeat it once through a fresh app session
            # so an interrupted request transaction does not strand an owned
            # user turn; deleted source/session access remains a no-op.
            await _persist_cancelled_after_disconnect(
                owner_user_id=owner_user_id,
                session_id=session_id,
                session_factory=app_session_factory,
            )
            # The fresh-session repair has completed (or safely no-op'd for a
            # deleted source/session). Only now can cleanup stop retrying it.
            terminal_emitted = True
            return (_event(ChatStreamEventType.DONE, {"session_id": str(session_id)}),)

    def _mark_persisted() -> None:
        nonlocal terminal_persisted
        terminal_persisted = True

    def _mark_emitted() -> None:
        nonlocal terminal_emitted
        terminal_emitted = True

    async def _persist_safe_delivery_failure() -> ChatMessage | None:
        """Record a failed result handoff without converting it into cancel.

        A Stage 4B query can finish before persisting the bounded transcript
        artefact fails.  That failure must withhold rows and remain visibly a
        persistence/delivery failure, rather than making the next generator
        finalizer write a misleading cancellation outcome.
        """

        nonlocal terminal_persisted
        try:
            await session_db.rollback()
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=(
                    "The database result could not be saved safely, so no result rows "
                    "were delivered."
                ),
                degraded=True,
                degraded_reason="result_persistence_failed",
            )
            terminal_persisted = True
            return assistant
        except Exception:
            # Source/session deletion and a failed persistence backend cannot
            # be repaired by inventing a transcript or exposing a result.
            return None

    async def _cleanup_interrupted_turn() -> None:
        """Stop remote work and repair a user turn closed at an SSE yield."""

        nonlocal cleanup_started
        if terminal_persisted or terminal_emitted or cleanup_started:
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
        # SPEC-09 W2.1. ``typed_only`` skips all of this and the turn is exactly the Stage 4C turn.
        fallthrough_allowed = False
        if settings.nl_sql_route_mode == "hybrid":
            if is_write_or_ddl_request(user_message):
                # R2: a write request reaches no model on either path.
                assistant = await _persist_assistant(
                    session_db=session_db, session_id=session_id, content=nl_messages.WRITE_REFUSAL, unsupported=True
                )
                terminal_persisted = True
                yield _event(
                    ChatStreamEventType.UNSUPPORTED,
                    {"message": nl_messages.WRITE_REFUSAL, "outcome": "unsupported", "snapshot_id": None},
                )
                yield _event(ChatStreamEventType.TOKEN, {"text": nl_messages.WRITE_REFUSAL})
                yield _done(assistant)
                return
            if not await _question_names_a_metric(
                session_db, owner_user_id=owner_user_id, source_id=session.source_id, question=user_message
            ):
                async for event in _stream_sql_path(
                    route="sql",
                    session_id=session_id,
                    owner_user_id=owner_user_id,
                    source_id=session.source_id,
                    user_message=user_message,
                    session_db=session_db,
                    settings=settings,
                    llm=llm,
                    cancellation=cancellation,
                    app_session_factory=app_session_factory,
                    messages=messages,
                    carried_usage=None,
                    set_persisted=_mark_persisted,
                    set_emitted=_mark_emitted,
                    cancelled_events=_cancelled_events,
                    sql_execute=sql_execute,
                    sql_execute_kwargs=sql_execute_kwargs,
                ):
                    yield event
                return
            fallthrough_allowed = True       # R3: a confirmed metric keeps the typed path
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

        if fallthrough_allowed and not planning.executable and planning.kind == "unsupported":
            # R3a: the typed path cannot do this (for example a join that is not an approved relationship). The
            # confirmed metric is in the schema glossary, so model SQL may still answer. Exactly once.
            async for event in _stream_sql_path(
                route="sql_after_metric_fallthrough",
                session_id=session_id,
                owner_user_id=owner_user_id,
                source_id=session.source_id,
                user_message=user_message,
                session_db=session_db,
                settings=settings,
                llm=llm,
                cancellation=cancellation,
                app_session_factory=app_session_factory,
                messages=messages,
                carried_usage=planning.token_usage,
                set_persisted=_mark_persisted,
                set_emitted=_mark_emitted,
                cancelled_events=_cancelled_events,
                sql_execute=sql_execute,
                sql_execute_kwargs=sql_execute_kwargs,
            ):
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
        if planning.metric_definition is not None:
            if not await _verify_metric_pin(
                metric_definition=planning.metric_definition,
                owner_user_id=owner_user_id,
                source_id=session.source_id,
                session_factory=app_session_factory,
            ):
                message = (
                    "The confirmed metric changed or needs review before execution. "
                    "No calculation was run."
                )
                assistant = await _persist_assistant(
                    session_db=session_db,
                    session_id=session_id,
                    content=message,
                    degraded=True,
                    degraded_reason="metric_pin_invalid",
                    token_usage=planning.token_usage,
                )
                terminal_persisted = True
                yield _event(ChatStreamEventType.ERROR, {"message": message})
                yield _event(ChatStreamEventType.TOKEN, {"text": message})
                yield _done(assistant, token_usage=planning.token_usage)
                return
        if planning.relationship_definition is not None:
            if not await _verify_relationship_pin(
                relationship_definition=planning.relationship_definition,
                owner_user_id=owner_user_id,
                source_id=session.source_id,
                session_factory=app_session_factory,
            ):
                message = (
                    "The approved relationship changed or needs review before execution. "
                    "No calculation was run."
                )
                assistant = await _persist_assistant(
                    session_db=session_db,
                    session_id=session_id,
                    content=message,
                    degraded=True,
                    degraded_reason="relationship_pin_invalid",
                    token_usage=planning.token_usage,
                )
                terminal_persisted = True
                yield _event(ChatStreamEventType.ERROR, {"message": message})
                yield _event(ChatStreamEventType.TOKEN, {"text": message})
                yield _done(assistant, token_usage=planning.token_usage)
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
            execution_kwargs: dict[str, Any] = {
                "owner_user_id": owner_user_id,
                "source_id": session.source_id,
                "snapshot_id": planning.snapshot_id,
                "plan": planning.plan,
                "settings": settings,
                "cancellation": cancellation,
            }
            if planning.metric_definition is not None and not cancellation.cancelled:
                # The executor accepts this optional immutable version-row
                # reference only together with a fresh trusted pin verifier.
                # It is not model output and is absent for ordinary Stage 4C
                # plans. The gateway invokes this closure immediately before
                # remote work, closing the edit/retire race after this
                # orchestration layer's initial precheck.
                metric_version_id = getattr(planning.metric_definition, "metric_version_id", None)
                if isinstance(metric_version_id, uuid.UUID):
                    execution_kwargs["metric_definition_version_id"] = metric_version_id

                    async def metric_pin_verifier() -> bool:
                        return await _verify_metric_pin(
                            metric_definition=planning.metric_definition,
                            owner_user_id=owner_user_id,
                            source_id=session.source_id,
                            session_factory=app_session_factory,
                        )

                    execution_kwargs["metric_pin_verifier"] = metric_pin_verifier
            if planning.relationship_definition is not None and not cancellation.cancelled:
                # The join plan contains an ID-only relationship pin. Bind it
                # to the independently resolved immutable version plus a
                # fresh verifier so the executor can reject a retirement,
                # review state, scope change, or cross-source provenance race.
                relationship_version_id = getattr(
                    planning.relationship_definition,
                    "relationship_version_id",
                    None,
                )
                if isinstance(relationship_version_id, uuid.UUID):
                    execution_kwargs[
                        "relationship_definition_version_id"
                    ] = relationship_version_id

                    async def relationship_pin_verifier() -> bool:
                        return await _verify_relationship_pin(
                            relationship_definition=planning.relationship_definition,
                            owner_user_id=owner_user_id,
                            source_id=session.source_id,
                            session_factory=app_session_factory,
                        )

                    execution_kwargs["relationship_pin_verifier"] = relationship_pin_verifier
            result = await execute(
                app_session_factory,
                **execution_kwargs,
            )
            if planning.metric_definition is not None and not cancellation.cancelled:
                # A metric edit, retirement, catalog refresh, or scope change
                # may race the remote read.  The run remains historical
                # provenance, but its rows are not persisted to chat or sent
                # to the browser once the pinned definition is no longer
                # current.
                if not await _verify_metric_pin(
                    metric_definition=planning.metric_definition,
                    owner_user_id=owner_user_id,
                    source_id=session.source_id,
                    session_factory=app_session_factory,
                ):
                    message = (
                        "The confirmed metric changed or needs review before its result "
                        "could be delivered. No result rows were shown."
                    )
                    assistant = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=message,
                        degraded=True,
                        degraded_reason="metric_pin_invalid",
                        token_usage=planning.token_usage,
                    )
                    terminal_persisted = True
                    yield _event(ChatStreamEventType.ERROR, {"message": message})
                    yield _event(ChatStreamEventType.TOKEN, {"text": message})
                    yield _done(assistant, token_usage=planning.token_usage)
                    return
                # This is safe definition provenance only: the service omits
                # fixed-filter values and does not duplicate rows.  It is
                # attached before transcript persistence/SSE serialization.
                result = result.model_copy(
                    update={
                        "metric_provenance": metric_service.metric_provenance(
                            planning.metric_definition
                        )
                    }
                )
            if planning.relationship_definition is not None and not cancellation.cancelled:
                # The gateway also performs this check, but chat is the final
                # row-bearing delivery boundary. Do not persist or stream a
                # result if the owner's approved edge changed after remote
                # execution completed.
                if not await _verify_relationship_pin(
                    relationship_definition=planning.relationship_definition,
                    owner_user_id=owner_user_id,
                    source_id=session.source_id,
                    session_factory=app_session_factory,
                ):
                    message = (
                        "The approved relationship changed or needs review before its "
                        "result could be delivered. No result rows were shown."
                    )
                    assistant = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=message,
                        degraded=True,
                        degraded_reason="relationship_pin_invalid",
                        token_usage=planning.token_usage,
                    )
                    terminal_persisted = True
                    yield _event(ChatStreamEventType.ERROR, {"message": message})
                    yield _event(ChatStreamEventType.TOKEN, {"text": message})
                    yield _done(assistant, token_usage=planning.token_usage)
                    return
                expected_relationship_id = getattr(
                    planning.relationship_definition, "relationship_id", None
                )
                expected_relationship_version_id = getattr(
                    planning.relationship_definition, "relationship_version_id", None
                )
                expected_version_number = getattr(
                    planning.relationship_definition, "version_number", None
                )
                expected_join_type = getattr(
                    planning.relationship_definition, "join_type", None
                )
                provenance = result.relationship_provenance
                if provenance is not None and (
                    provenance.relationship_id != expected_relationship_id
                    or provenance.relationship_version_id != expected_relationship_version_id
                    or provenance.version_number != expected_version_number
                    or provenance.join_type != expected_join_type
                ):
                    message = (
                        "The approved relationship changed before its result could be "
                        "delivered. No result rows were shown."
                    )
                    assistant = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=message,
                        degraded=True,
                        degraded_reason="relationship_pin_invalid",
                        token_usage=planning.token_usage,
                    )
                    terminal_persisted = True
                    yield _event(ChatStreamEventType.ERROR, {"message": message})
                    yield _event(ChatStreamEventType.TOKEN, {"text": message})
                    yield _done(assistant, token_usage=planning.token_usage)
                    return
                if provenance is None and (
                    isinstance(expected_relationship_id, uuid.UUID)
                    and isinstance(expected_relationship_version_id, uuid.UUID)
                    and isinstance(expected_version_number, int)
                    and expected_join_type in {"inner", "left"}
                ):
                    # Test/delivery adapters can return a typed success before
                    # adding compiler provenance. The planner's independently
                    # pinned relationship is safe to attach only after the
                    # recheck above; production gateway results already carry
                    # the same compiler-originated record.
                    result = result.model_copy(
                        update={
                            "relationship_provenance": PostgresRelationshipProvenance(
                                relationship_id=expected_relationship_id,
                                relationship_version_id=expected_relationship_version_id,
                                version_number=expected_version_number,
                                join_type=expected_join_type,
                            )
                        }
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
            terminal_emitted = True
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

        # Revalidate semantic pins immediately before the one row-bearing
        # transcript write. The gateway checks them too, but a metric or
        # relationship can still be retired/revised between its return and
        # this final delivery decision.
        if planning.metric_definition is not None and not await _verify_metric_pin(
            metric_definition=planning.metric_definition,
            owner_user_id=owner_user_id,
            source_id=session.source_id,
            session_factory=app_session_factory,
        ):
            message = (
                "The confirmed metric changed or needs review before its result could "
                "be delivered. No result rows were shown."
            )
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=message,
                degraded=True,
                degraded_reason="metric_pin_invalid",
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            yield _done(assistant, token_usage=planning.token_usage)
            return
        if planning.relationship_definition is not None and not await _verify_relationship_pin(
            relationship_definition=planning.relationship_definition,
            owner_user_id=owner_user_id,
            source_id=session.source_id,
            session_factory=app_session_factory,
        ):
            message = (
                "The approved relationship changed or needs review before its result "
                "could be delivered. No result rows were shown."
            )
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=message,
                degraded=True,
                degraded_reason="relationship_pin_invalid",
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            yield _done(assistant, token_usage=planning.token_usage)
            return

        summary = _summary_for_result(result)
        binding = _delivery_binding_for_result(
            result, owner_user_id=owner_user_id, source_id=session.source_id
        )
        if binding is None:
            # Results supplied by the production executor always carry a
            # non-serializable binding.  Without it there is no safe way to
            # prove that scope/credential state still authorizes delivery.
            terminal_emitted = True
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "Access changed before the database result could be delivered."},
            )
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
            return
        # Atomically order a cancel request and the final row-bearing write.
        # A cancellation already accepted means we never retain the result;
        # one accepted later is still checked before SSE disclosure below.
        delivery_allowed = False
        assistant: ChatMessage | None = None
        async with cancellation.delivery_guard() as delivery_allowed:
            if delivery_allowed:
                assistant = await _persist_postgres_result_with_delivery_guard(
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    source_id=session.source_id,
                    result=result,
                    content=summary,
                    settings=settings,
                    session_factory=app_session_factory,
                    degraded=not result.is_ok,
                    degraded_reason=result.failure_code,
                    token_usage=planning.token_usage,
                )
                if assistant is not None:
                    terminal_persisted = True
        if not delivery_allowed:
            for event in await _cancelled_events():
                yield event
            return
        if assistant is None:
            terminal_emitted = True
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "Access changed before the database result could be delivered."},
            )
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
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
            terminal_emitted = True
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

        # The source row remains locked through the actual row-bearing yield.
        # A scope or credential mutation therefore either committed first (and
        # invalidates the binding) or waits until this frame is delivered.  It
        # is not enough to read authorization immediately before yielding: the
        # mutation could otherwise commit in that check-to-yield window.
        sse_allowed = False
        try:
            async with postgres_query_delivery_guard(
                app_session_factory,
                owner_user_id=owner_user_id,
                binding=binding,
                settings=settings,
            ) as delivery_session:
                conversation = await manager.get_session_for_access(
                    delivery_session,
                    session_id,
                    access=manager.ConversationAccess.for_user(owner_user_id),
                )
                if (
                    conversation.session.source_id == session.source_id
                    and conversation.source is not None
                    and conversation.source.id == session.source_id
                    and not cancellation.cancelled
                ):
                    safe_result = result.model_dump(mode="json")
                    sse_allowed = True
                    terminal_emitted = True
                    yield _event(ChatStreamEventType.POSTGRES_RESULT, {"postgres_result": safe_result})
        except asyncio.CancelledError:
            raise
        except Exception:
            sse_allowed = False
        if not sse_allowed:
            terminal_emitted = True
            if cancellation.cancelled:
                yield _event(
                    ChatStreamEventType.ERROR,
                    {"message": "The database calculation was cancelled before its result could be delivered."},
                )
                yield _done(assistant, token_usage=planning.token_usage)
            else:
                yield _event(
                    ChatStreamEventType.ERROR,
                    {"message": "Access changed before the database result could be delivered."},
                )
                yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
            return
        yield _event(ChatStreamEventType.TOKEN, {"text": summary})
        yield _done(assistant, token_usage=planning.token_usage)
    except asyncio.CancelledError:
        await _cleanup_interrupted_turn()
        raise
    except Exception:
        # All expected planner/executor failures normally return typed
        # outcomes above.  This catches only an unexpected orchestration or
        # result-persistence fault after the committed user message.  It must
        # never disclose a buffered result or label the failure as a user
        # cancellation.
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
        else:
            message = "The database result could not be delivered safely, so no answer was generated."
            assistant = await _persist_safe_delivery_failure()
            # If persistence was interrupted, leave this false so the
            # cancellation/finalizer path can still repair the committed user
            # turn. A completed persistence or a completed no-op may safely
            # suppress that duplicate cleanup.
            terminal_emitted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            if assistant is not None:
                yield _done(assistant)
            else:
                yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
    finally:
        # Routes may close an async generator directly after an SSE frame,
        # which runs ``finally`` without raising CancelledError at the inner
        # executor await. This repairs a committed user turn in that path too.
        await _cleanup_interrupted_turn()


__all__ = ["stream_postgres_chat_turn"]
