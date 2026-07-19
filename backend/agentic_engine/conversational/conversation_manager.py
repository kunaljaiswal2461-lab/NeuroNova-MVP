"""Persistence layer for chat sessions and messages.

Thin async CRUD on top of the SQLAlchemy ORM models in
:mod:`app.db.models.conversation`. The chat agent uses this module as
its only persistence touchpoint — it never talks to the ORM directly,
which keeps the orchestrator free of session-handling boilerplate.

Three responsibilities:

  * Create / fetch / list / delete :class:`ConversationSession`s.
  * Append a :class:`ChatMessage` while keeping the denormalised
    ``last_active_at`` and ``message_count`` columns in sync.
  * Read the rolling window of recent messages used as LLM context
    (capped at :data:`MAX_HISTORY_MESSAGES`).

All functions accept the active :class:`AsyncSession` and never commit
on their own — the caller (route handler or chat agent) owns the
transaction boundary so retries and rollbacks compose cleanly.
Exception: :func:`create_session` commits at the end because it is
typically called from a route that just wants the session id and has no
other work to bundle.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.conversation import (
    ChatMessageRow,
    ChatRoleDB,
    ConversationModeDB,
    ConversationSessionRow,
)
from app.exceptions.custom_exceptions import NotFoundError
from agentic_engine.conversational.models import (
    MAX_HISTORY_MESSAGES,
    ChatMessage,
    ChatRole,
    ConversationMode,
    ConversationSession,
    IntentDecision,
    QueryResult,
    RagCitation,
)
from agentic_engine.llm_engine.models import TokenUsage


logger = get_logger("conversational.manager")


# ── sessions ────────────────────────────────────────────────────────────────

async def create_session(
    *,
    session_db: AsyncSession,
    dataset_id: uuid.UUID,
    mode: ConversationMode = ConversationMode.AUTO,
    user_id: uuid.UUID | None = None,
) -> ConversationSession:
    """Create a new conversation session for a dataset.

    Commits on success so the caller can immediately hand the new
    session id back to the client.
    """
    row = ConversationSessionRow(
        dataset_id=dataset_id,
        mode=ConversationModeDB(mode.value),
        user_id=user_id,
    )
    session_db.add(row)
    await session_db.commit()
    await session_db.refresh(row)

    logger.info(
        "conversation.session_created",
        session_id=str(row.id),
        dataset_id=str(dataset_id),
        mode=mode.value,
    )
    return _row_to_session(row)


async def get_session(
    session_db: AsyncSession, session_id: uuid.UUID
) -> ConversationSession:
    """Fetch a session by id or raise :class:`NotFoundError`."""
    row = await _get_session_row(session_db, session_id)
    return _row_to_session(row)


async def list_sessions_for_dataset(
    session_db: AsyncSession,
    dataset_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[ConversationSession]:
    """List a dataset's sessions, most-recently-active first."""
    stmt = (
        select(ConversationSessionRow)
        .where(ConversationSessionRow.dataset_id == dataset_id)
        .order_by(ConversationSessionRow.last_active_at.desc())
        .limit(limit)
    )
    result = await session_db.execute(stmt)
    return [_row_to_session(row) for row in result.scalars().all()]


async def delete_session(
    session_db: AsyncSession, session_id: uuid.UUID
) -> None:
    """Hard-delete a session and (via FK cascade) all its messages."""
    row = await _get_session_row(session_db, session_id)
    await session_db.delete(row)
    await session_db.commit()
    logger.info("conversation.session_deleted", session_id=str(session_id))


# ── messages ────────────────────────────────────────────────────────────────

async def append_message(
    *,
    session_db: AsyncSession,
    session_id: uuid.UUID,
    role: ChatRole,
    content: str,
    mode_used: ConversationMode | None = None,
    intent: IntentDecision | None = None,
    citations: list[RagCitation] | None = None,
    query_result: QueryResult | None = None,
    degraded: bool = False,
    degraded_reason: str | None = None,
    token_usage: TokenUsage | None = None,
    commit: bool = True,
) -> ChatMessage:
    """Persist one message and update the session's denormalised counters.

    ``commit`` is True by default so route handlers don't have to
    remember to commit; the chat agent passes ``commit=False`` while
    streaming and commits once at the end of a turn so user + assistant
    rows land atomically.
    """
    session_row = await _get_session_row(session_db, session_id)

    message_row = ChatMessageRow(
        session_id=session_id,
        role=ChatRoleDB(role.value),
        content=content,
        mode_used=mode_used.value if mode_used else None,
        intent=intent.model_dump(mode="json") if intent else None,
        citations=(
            [c.model_dump(mode="json") for c in citations]
            if citations
            else None
        ),
        query_result=query_result.model_dump(mode="json") if query_result else None,
        degraded=degraded,
        degraded_reason=degraded_reason,
        token_input=token_usage.input_tokens if token_usage else None,
        token_output=token_usage.output_tokens if token_usage else None,
    )
    session_db.add(message_row)

    session_row.message_count += 1
    session_row.last_active_at = datetime.now(timezone.utc)

    await session_db.flush()
    if commit:
        await session_db.commit()
        await session_db.refresh(message_row)

    return _row_to_message(message_row)


async def get_recent_messages(
    session_db: AsyncSession,
    session_id: uuid.UUID,
    *,
    limit: int = MAX_HISTORY_MESSAGES,
) -> list[ChatMessage]:
    """Read the rolling window of recent messages, oldest-first.

    The query orders DESC + LIMIT in SQL (cheap thanks to the composite
    index on ``(session_id, created_at)``), then re-reverses in Python
    so the LLM context flows in chronological order without paying for
    an ASC index scan over the whole table.
    """
    stmt = (
        select(ChatMessageRow)
        .where(ChatMessageRow.session_id == session_id)
        .order_by(ChatMessageRow.created_at.desc())
        .limit(limit)
    )
    result = await session_db.execute(stmt)
    rows = list(result.scalars().all())
    rows.reverse()
    return [_row_to_message(row) for row in rows]


async def count_messages(
    session_db: AsyncSession, session_id: uuid.UUID
) -> int:
    """Total messages on a session (used by tests and cost reporting)."""
    stmt = select(func.count(ChatMessageRow.id)).where(
        ChatMessageRow.session_id == session_id
    )
    result = await session_db.execute(stmt)
    return int(result.scalar() or 0)


# ── history helpers used by the chat agent ──────────────────────────────────

def history_for_prompt(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Convert persisted messages into the ``[{role, content}]`` shape
    the streaming LLM call expects.

    Drops degraded/empty rows and clamps each message to a safety limit
    so a single pathological turn cannot blow the prompt budget. We
    don't truncate aggressively because the upstream
    :data:`MAX_HISTORY_MESSAGES` cap already bounds the total.
    """
    _PER_MESSAGE_CHAR_CAP: int = 4000
    out: list[dict[str, str]] = []
    for msg in messages:
        content = (msg.content or "").strip()
        if not content:
            continue
        out.append({
            "role": "assistant" if msg.role is ChatRole.ASSISTANT else "user",
            "content": content[:_PER_MESSAGE_CHAR_CAP],
        })
    return out


# ── internals ───────────────────────────────────────────────────────────────

async def _get_session_row(
    session_db: AsyncSession, session_id: uuid.UUID
) -> ConversationSessionRow:
    row = await session_db.get(ConversationSessionRow, session_id)
    if row is None:
        raise NotFoundError(
            f"conversation session {session_id} not found",
            details={"session_id": str(session_id)},
        )
    return row


def _row_to_session(row: ConversationSessionRow) -> ConversationSession:
    return ConversationSession(
        session_id=row.id,
        dataset_id=row.dataset_id,
        mode=ConversationMode(row.mode.value),
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        message_count=row.message_count,
    )


def _row_to_message(row: ChatMessageRow) -> ChatMessage:
    intent = _decode_intent(row.intent)
    citations = _decode_citations(row.citations)
    query_result = _decode_query_result(row.query_result)
    token_usage = _decode_token_usage(row.token_input, row.token_output)

    return ChatMessage(
        message_id=row.id,
        session_id=row.session_id,
        role=ChatRole(row.role.value),
        content=row.content,
        created_at=row.created_at,
        mode_used=ConversationMode(row.mode_used) if row.mode_used else None,
        intent=intent,
        citations=citations,
        query_result=query_result,
        token_usage=token_usage,
        degraded=row.degraded,
        degraded_reason=row.degraded_reason,
    )


def _decode_intent(blob: dict[str, Any] | None) -> IntentDecision | None:
    if not blob:
        return None
    try:
        return IntentDecision.model_validate(blob)
    except Exception as exc:  # pragma: no cover — schema drift guard
        logger.warning("conversation.intent_decode_failed", error=str(exc))
        return None


def _decode_citations(blob: list[dict[str, Any]] | None) -> list[RagCitation]:
    if not blob:
        return []
    out: list[RagCitation] = []
    for item in blob:
        try:
            out.append(RagCitation.model_validate(item))
        except Exception as exc:  # pragma: no cover — schema drift guard
            logger.warning("conversation.citation_decode_failed", error=str(exc))
    return out


def _decode_query_result(blob: dict[str, Any] | None) -> QueryResult | None:
    if not blob:
        return None
    try:
        return QueryResult.model_validate(blob)
    except Exception as exc:  # pragma: no cover — schema drift guard
        logger.warning("conversation.query_result_decode_failed", error=str(exc))
        return None


def _decode_token_usage(
    input_tokens: int | None, output_tokens: int | None
) -> TokenUsage | None:
    if input_tokens is None and output_tokens is None:
        return None
    usage = TokenUsage()
    usage.add(input_tokens or 0, output_tokens or 0)
    return usage
