"""Chat agent — Layer 7 orchestrator.

Owns one chat turn end-to-end:

  1. Load session, dataset, and history.
  2. Persist the user message.
  3. Classify intent (session pin → heuristic → LLM fallback).
  4. Route to the RAG branch or the QUERY branch.
  5. Stream the result back as a sequence of typed
     :class:`ChatStreamEvent`s.
  6. Persist the assistant message with citations / query result /
     token usage attached, then commit the turn atomically.

The agent is implemented as an async generator. The route layer wraps
its output in an SSE response so the chat UI can render tokens as they
arrive. Every event is structurally typed so the wire format is easy
to evolve without breaking older clients.

Failure handling: the agent never raises out of the stream. Every
known failure mode (no LLM, no embedder, missing dataset, AST
rejection, runtime error, mid-stream API failure) ends with an
``error`` event followed by a ``done`` event, and the assistant turn
is persisted in degraded mode so the transcript stays consistent with
what the user saw.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.dataset import DatasetRecord, DatasetStatus
from app.services import dataset_service
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.intent_classifier import classify_intent
from agentic_engine.conversational.models import (
    ChatMessage,
    ChatRole,
    ChatStreamEvent,
    ChatStreamEventType,
    ConversationMode,
    IntentDecision,
    QueryResult,
    RagCitation,
)
from agentic_engine.conversational.query_executor import (
    execute_query_plan,
    schema_snapshot_from_profile,
)
from agentic_engine.conversational.rag_answerer import stream_rag_answer
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable, StreamDelta
from agentic_engine.llm_engine.models import TokenUsage
from agentic_engine.llm_engine.openai_client import build_client
from agentic_engine.profiler.engine import load_profile_report
from agentic_engine.profiler.report import ProfileReport
from agentic_engine.vector_store.embedder import Embedder, build_embedder


logger = get_logger("conversational.agent")


# ── public entry point ──────────────────────────────────────────────────────

async def stream_chat_turn(
    *,
    session_id: uuid.UUID,
    user_message: str,
    session_db: AsyncSession,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Stream one chat turn — yields typed :class:`ChatStreamEvent`s.

    The function takes ownership of all persistence for the turn: it
    writes the user message at the start and the assistant message
    (with all artefacts attached) at the end. Callers (the route
    handler) just pump the events to the client.
    """
    settings = settings or get_settings()
    # Build the LLM + embedder once per turn so test fixtures can
    # inject fakes; default to the production OpenAI implementations.
    eff_llm = llm if llm is not None else build_client(settings.openai_api_key)
    eff_embedder = (
        embedder if embedder is not None else build_embedder(settings.openai_api_key)
    )

    # ── 1. Load session + dataset metadata. ───────────────────────────────
    session = await manager.get_session(session_db, session_id)
    dataset_record = await dataset_service.get_dataset(
        session_db, session.dataset_id
    )

    if dataset_record.status is not DatasetStatus.COMPLETE:
        # Dataset is still being processed — refuse the turn rather than
        # answering with partial context. We still persist the user
        # message so the transcript stays consistent.
        async for event in _refuse_not_ready(
            session_db=session_db,
            session_id=session_id,
            user_message=user_message,
            status=dataset_record.status,
        ):
            yield event
        return

    # ── 2. Persist user message (no commit yet — we batch the turn). ─────
    user_msg = await manager.append_message(
        session_db=session_db,
        session_id=session_id,
        role=ChatRole.USER,
        content=user_message,
        commit=False,
    )

    yield _event(
        ChatStreamEventType.START,
        {
            "session_id": str(session_id),
            "user_message_id": str(user_msg.message_id),
        },
    )

    # ── 3. Build the per-turn context (history + dataset schema). ────────
    history_messages = await manager.get_recent_messages(
        session_db, session_id
    )
    # The last message in history is the just-persisted user turn; drop it
    # so the LLM doesn't see it twice (once as history, once as ``user``).
    history_for_llm = manager.history_for_prompt(history_messages[:-1])

    try:
        profile_columns, overview, profile = await asyncio.to_thread(
            _load_dataset_context, dataset_record, settings
        )
    except DatasetProfileUnavailable as exc:
        async for event in _refuse_profile_unavailable(
            session_db=session_db,
            session_id=session_id,
            reason=str(exc),
        ):
            yield event
        return

    # ── 4. Classify intent. ──────────────────────────────────────────────
    schema_snapshot = schema_snapshot_from_profile(profile_columns)
    intent = await classify_intent(
        user_message,
        session_mode=session.mode,
        schema_snapshot=schema_snapshot,
        llm=eff_llm,
        settings=settings,
    )

    yield _event(
        ChatStreamEventType.INTENT,
        {
            "mode": intent.mode,
            "confidence": intent.confidence,
            "rationale": intent.rationale,
            "routed_by": intent.routed_by.value,
        },
    )

    # ── 5. Dispatch to the branch handler. ───────────────────────────────
    try:
        if intent.mode == "RAG":
            async for event, finalisation in _run_rag_branch(
                session_id=session_id,
                user_message=user_message,
                dataset_id=session.dataset_id,
                overview=overview,
                history=history_for_llm,
                session_db=session_db,
                settings=settings,
                llm=eff_llm,
                embedder=eff_embedder,
            ):
                if event is not None:
                    yield event
                if finalisation is not None:
                    citations, content, token_usage, degraded, reason = finalisation
                    assistant_msg = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=content,
                        mode_used=ConversationMode.RAG,
                        intent=intent,
                        citations=citations,
                        query_result=None,
                        token_usage=token_usage,
                        degraded=degraded,
                        degraded_reason=reason,
                    )
                    yield _done_event(assistant_msg, intent, token_usage)
        else:  # QUERY
            async for event, finalisation in _run_query_branch(
                session_id=session_id,
                user_message=user_message,
                dataset_record=dataset_record,
                profile_columns=profile_columns,
                profile=profile,
                history=history_for_llm,
                settings=settings,
                llm=eff_llm,
            ):
                if event is not None:
                    yield event
                if finalisation is not None:
                    (
                        query_result,
                        content,
                        token_usage,
                        degraded,
                        reason,
                        unsupported,
                    ) = finalisation
                    assistant_msg = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=content,
                        mode_used=ConversationMode.QUERY,
                        intent=intent,
                        citations=[],
                        query_result=query_result,
                        token_usage=token_usage,
                        degraded=degraded,
                        degraded_reason=reason,
                        unsupported=unsupported,
                    )
                    yield _done_event(assistant_msg, intent, token_usage)
    except Exception as exc:  # pragma: no cover — top-level safety net
        # Anything that escapes the branch handlers gets persisted as a
        # degraded assistant turn so the transcript still makes sense.
        logger.exception(
            "chat.unhandled_error",
            session_id=str(session_id),
            error=str(exc),
        )
        assistant_msg = await _persist_assistant(
            session_db=session_db,
            session_id=session_id,
            content=f"(internal error: {exc})",
            mode_used=ConversationMode(intent.mode),
            intent=intent,
            citations=[],
            query_result=None,
            token_usage=None,
            degraded=True,
            degraded_reason=f"unhandled: {exc}",
        )
        yield _event(
            ChatStreamEventType.ERROR,
            {"message": str(exc)},
        )
        yield _done_event(assistant_msg, intent, None)


# ── branch: RAG ─────────────────────────────────────────────────────────────

async def _run_rag_branch(
    *,
    session_id: uuid.UUID,
    user_message: str,
    dataset_id: uuid.UUID,
    overview: dict[str, Any],
    history: list[dict[str, str]],
    session_db: AsyncSession,
    settings: Settings,
    llm: LLMClient | None,
    embedder: Embedder | None,
) -> AsyncIterator[tuple[ChatStreamEvent | None, tuple | None]]:
    """Stream the RAG branch.

    Yields ``(event, finalisation)`` tuples where exactly one of the
    two is non-None per tick. The finalisation tuple, when present,
    carries everything the orchestrator needs to persist the assistant
    message:

        ``(citations, content, token_usage, degraded, degraded_reason)``
    """
    buffered_text: list[str] = []
    sent_citations = False
    citations: list[RagCitation] = []
    token_usage = TokenUsage()
    degraded = False
    degraded_reason: str | None = None

    async for citation_snapshot, delta in stream_rag_answer(
        user_message,
        dataset_id=dataset_id,
        overview=overview,
        history=history,
        settings=settings,
        llm=llm,
        embedder=embedder,
        db_session=session_db,
    ):
        # Emit the citations event once, just before the first text frame.
        if not sent_citations:
            citations = citation_snapshot
            yield _event(
                ChatStreamEventType.CITATIONS,
                {"citations": [c.model_dump(mode="json") for c in citations]},
            ), None
            sent_citations = True

        if delta.finished:
            token_usage.add(delta.input_tokens, delta.output_tokens)
            content = "".join(buffered_text).strip()
            # A missing LLM produces a user-visible fallback answer rather
            # than an exception, but it is still a degraded turn.
            if llm is None:
                degraded = True
                degraded_reason = "LLM client is not configured"
            if not content:
                # RAG layer can return zero text deltas in degraded paths
                # — keep the assistant turn non-empty so the transcript
                # remains readable.
                content = (
                    "(no answer was produced; please try rephrasing or "
                    "switch to direct-query mode)"
                )
                degraded = True
                degraded_reason = "rag produced no text"
            yield None, (
                citations,
                content,
                token_usage,
                degraded,
                degraded_reason,
            )
            # No explicit return — let the inner generator exhaust on its
            # next iteration so its async finaliser runs cleanly. If we
            # returned here, Python would have to gc-close the inner
            # generator and emit a "coroutine was never awaited" warning.
        elif delta.text:
            buffered_text.append(delta.text)
            yield _event(
                ChatStreamEventType.TOKEN, {"text": delta.text}
            ), None


# ── branch: NL → Polars ─────────────────────────────────────────────────────

async def _run_query_branch(
    *,
    session_id: uuid.UUID,
    user_message: str,
    dataset_record: DatasetRecord,
    profile_columns: list[dict[str, Any]],
    profile: ProfileReport,
    history: list[dict[str, str]],
    settings: Settings,
    llm: LLMClient | None,
) -> AsyncIterator[tuple[ChatStreamEvent | None, tuple | None]]:
    """Run the NL→Polars branch.

    Differs from RAG: the bulk of the work is one codegen call + one
    sandboxed eval, neither of which streams meaningfully. We emit a
    QUERY_RESULT event up-front, then a small natural-language summary
    as a single TOKEN event (synthesised here, no second LLM call).
    """
    raw_path = settings.raw_dir / dataset_record.filename
    schema_snapshot = schema_snapshot_from_profile(profile_columns)

    execution = await execute_query_plan(
        user_message,
        raw_path=raw_path,
        file_type=dataset_record.file_type,
        profile=profile,
        schema_snapshot=schema_snapshot,
        history=history,
        llm=llm,
        settings=settings,
    )

    query_result = execution.query_result
    if query_result is not None:
        yield _event(
            ChatStreamEventType.QUERY_RESULT,
            {
                "query_result": query_result.model_dump(mode="json"),
                "generated_code": query_result.expression or None,
            },
        ), None

    # Compose a terse natural-language summary client-side. We
    # deliberately don't make a second LLM call here — the table is the
    # answer; the prose is just a one-line caption.
    summary = execution.reply or _summarise_query_result(query_result)
    yield _event(ChatStreamEventType.TOKEN, {"text": summary}), None

    yield None, (
        query_result,
        summary,
        execution.token_usage,
        execution.degraded,
        execution.degraded_reason,
        execution.unsupported,
    )


def _summarise_query_result(result: QueryResult | None) -> str:
    """One-line caption rendered alongside the result table."""
    if result is None:
        return "No direct-query result was produced."
    if not result.is_ok:
        attempts = (
            f" after {result.attempts} attempts" if result.attempts > 1 else ""
        )
        return (
            f"Could not run the query{attempts}: {result.error}. "
            "Try rephrasing the question or naming the exact columns."
        )
    if result.row_count == 0:
        return "Ran the query — no rows matched."
    suffix = (
        f" (showing first {len(result.rows)} of {result.row_count})"
        if result.truncated
        else ""
    )
    return (
        f"Computed {result.row_count} row(s) across "
        f"{len(result.columns)} column(s){suffix}."
    )


# ── refusal path (dataset not ready) ────────────────────────────────────────

async def _refuse_not_ready(
    *,
    session_db: AsyncSession,
    session_id: uuid.UUID,
    user_message: str,
    status: DatasetStatus,
) -> AsyncIterator[ChatStreamEvent]:
    """Used when the dataset is still processing at chat time.

    Persists the user message and a degraded assistant message
    explaining the refusal, then streams the same explanation as a
    single TOKEN event so the UI looks normal.
    """
    user_msg = await manager.append_message(
        session_db=session_db,
        session_id=session_id,
        role=ChatRole.USER,
        content=user_message,
        commit=False,
    )
    yield _event(
        ChatStreamEventType.START,
        {
            "session_id": str(session_id),
            "user_message_id": str(user_msg.message_id),
        },
    )

    explanation = (
        f"The dataset is still being processed (status: {status.value}). "
        "Please wait for it to reach COMPLETE before chatting."
    )
    yield _event(ChatStreamEventType.TOKEN, {"text": explanation})

    assistant_msg = await _persist_assistant(
        session_db=session_db,
        session_id=session_id,
        content=explanation,
        mode_used=None,
        intent=None,
        citations=[],
        query_result=None,
        token_usage=None,
        degraded=True,
        degraded_reason=f"dataset not ready ({status.value})",
    )
    yield _done_event(assistant_msg, None, None)


async def _refuse_profile_unavailable(
    *,
    session_db: AsyncSession,
    session_id: uuid.UUID,
    reason: str,
) -> AsyncIterator[ChatStreamEvent]:
    """Persist a clear degraded response before streaming any refusal event."""
    assistant_msg = await _persist_assistant(
        session_db=session_db,
        session_id=session_id,
        content=reason,
        mode_used=None,
        intent=None,
        citations=[],
        query_result=None,
        token_usage=None,
        degraded=True,
        degraded_reason="dataset profile unavailable",
    )
    yield _event(ChatStreamEventType.TOKEN, {"text": reason})
    yield _done_event(assistant_msg, None, None)


# ── persistence helpers ─────────────────────────────────────────────────────

async def _persist_assistant(
    *,
    session_db: AsyncSession,
    session_id: uuid.UUID,
    content: str,
    mode_used: ConversationMode | None,
    intent: IntentDecision | None,
    citations: list[RagCitation],
    query_result: QueryResult | None,
    token_usage: TokenUsage | None,
    degraded: bool,
    degraded_reason: str | None,
    unsupported: bool = False,
) -> ChatMessage:
    """Persist the assistant turn and commit the whole transaction."""
    message = await manager.append_message(
        session_db=session_db,
        session_id=session_id,
        role=ChatRole.ASSISTANT,
        content=content,
        mode_used=mode_used,
        intent=intent,
        citations=citations,
        query_result=query_result,
        degraded=degraded,
        degraded_reason=degraded_reason,
        unsupported=unsupported,
        token_usage=token_usage,
        commit=True,
    )
    # The persistence layer reconstructs this from QueryResult.expression on
    # transcript reads. Set it here too so the live SSE response carries the
    # same display-only value without changing prompt history.
    message.generated_code = (query_result.expression or None) if query_result else None
    message.unsupported = unsupported
    return message


# ── dataset context loading ─────────────────────────────────────────────────

_SCHEMA_CONTEXT_COLUMN_LIMIT = 200


class DatasetProfileUnavailable(RuntimeError):
    """Raised when a completed dataset has no usable persisted profile."""


def _load_dataset_context(
    dataset_record: DatasetRecord, settings: Settings
) -> tuple[list[dict[str, Any]], dict[str, Any], ProfileReport]:
    """Create compact chat context from the canonical persisted profile.

    The profile model owns both its path and JSON shape. This deliberately
    returns schema and aggregate profile facts only; raw dataset rows never
    enter either the query or RAG prompt.
    """
    try:
        profile = load_profile_report(dataset_record.id, settings)
    except Exception as exc:
        logger.error(
            "chat.profile_invalid",
            dataset_id=str(dataset_record.id),
            error=str(exc),
        )
        raise DatasetProfileUnavailable(
            "The dataset profile is invalid. Please re-upload the dataset before chatting."
        ) from exc

    if profile is None:
        raise DatasetProfileUnavailable(
            "The dataset profile is unavailable. Please re-upload the dataset before chatting."
        )

    return _schema_snapshot(profile), _overview_snapshot(profile), profile


def _schema_snapshot(profile: ProfileReport) -> list[dict[str, Any]]:
    """Return typed column metadata plus safe aggregate quality facts."""
    semantic_by_name = {
        tag.name: tag.semantic_type for tag in profile.semantic.columns
    }
    stats_by_name = {stat.name: stat for stat in profile.stats.columns}
    quality_by_name = {quality.name: quality for quality in profile.quality.columns}

    columns: list[dict[str, Any]] = []
    all_columns = profile.schema_.columns
    for column in all_columns[:_SCHEMA_CONTEXT_COLUMN_LIMIT]:
        quality = quality_by_name.get(column.name)
        stats = stats_by_name.get(column.name)
        snapshot: dict[str, Any] = {
            "name": column.name,
            # Use Polars' actual dtype for code generation while retaining the
            # profile's logical dtype for descriptive consumers.
            "dtype": column.polars_dtype,
            "logical_dtype": column.dtype,
            "polars_dtype": column.polars_dtype,
            "nullable": column.nullable,
            "semantic_type": semantic_by_name.get(column.name, "UNKNOWN"),
        }
        if stats is not None:
            snapshot["inferred_kind"] = stats.inferred_kind
            if stats.categorical is not None:
                snapshot["cardinality"] = stats.categorical.cardinality
        if quality is not None:
            snapshot["null_pct"] = quality.null_pct
            snapshot["is_constant"] = quality.is_constant
        columns.append(snapshot)
    if len(all_columns) > _SCHEMA_CONTEXT_COLUMN_LIMIT:
        columns.append(
            {
                "schema_truncated": True,
                "included_column_count": _SCHEMA_CONTEXT_COLUMN_LIMIT,
                "total_column_count": len(all_columns),
            }
        )
    return columns


def _overview_snapshot(profile: ProfileReport) -> dict[str, Any]:
    """Return a bounded RAG overview derived solely from aggregate facts."""
    semantic_counts: dict[str, int] = {}
    for tag in profile.semantic.columns:
        semantic_counts[tag.semantic_type] = (
            semantic_counts.get(tag.semantic_type, 0) + 1
        )

    null_columns = sum(
        1 for column in profile.quality.columns if column.null_pct > 0
    )
    constant_columns = sum(
        1 for column in profile.quality.columns if column.is_constant
    )
    kind_counts: dict[str, int] = {}
    for column in profile.stats.columns:
        kind_counts[column.inferred_kind] = (
            kind_counts.get(column.inferred_kind, 0) + 1
        )

    return {
        "row_count": profile.schema_.row_count,
        "column_count": profile.schema_.col_count,
        "health": {
            "score": profile.health.score,
            "grade": profile.health.grade,
            "components": profile.health.components,
        },
        "duplicate_row_pct": profile.quality.duplicate_row_pct,
        "columns_with_nulls": null_columns,
        "constant_columns": constant_columns,
        "semantic_type_counts": semantic_counts,
        "inferred_kind_counts": kind_counts,
    }


# ── event builders ──────────────────────────────────────────────────────────

def _event(event_type: ChatStreamEventType, data: dict[str, Any]) -> ChatStreamEvent:
    return ChatStreamEvent(event=event_type, data=data)


def _done_event(
    assistant_message: ChatMessage,
    intent: IntentDecision | None,
    token_usage: TokenUsage | None,
) -> ChatStreamEvent:
    """Terminal event — always last, even on error.

    The UI uses this to capture the final message id (so it can attach
    citations / re-render the table once the stream closes) and to
    update its cost-meter without a separate round-trip.
    """
    payload: dict[str, Any] = {
        "message_id": str(assistant_message.message_id),
        "session_id": str(assistant_message.session_id),
        "degraded": assistant_message.degraded,
        "degraded_reason": assistant_message.degraded_reason,
        "generated_code": assistant_message.generated_code,
        "unsupported": assistant_message.unsupported,
    }
    if intent is not None:
        payload["intent"] = intent.model_dump(mode="json")
    if token_usage is not None:
        payload["token_usage"] = token_usage.model_dump(mode="json")
    return _event(ChatStreamEventType.DONE, payload)
