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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.conversation import (
    ChatMessageRow,
    ChatRoleDB,
    ConversationModeDB,
    ConversationSessionRow,
)
from app.db.models.dataset import DatasetRecord, DatasetStatus
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


@dataclass(frozen=True)
class ConversationAccess:
    """Trusted caller identity used to scope conversation operations.

    A JWT user owns conversations through their ``user_id``. A legacy global
    API key has no user identity, so it is deliberately restricted to the
    pre-authentication namespace where both the dataset and conversation are
    unowned. ``None`` must never mean "all users" here.
    """

    user_id: uuid.UUID | None
    legacy_api_key: bool

    def __post_init__(self) -> None:
        if self.legacy_api_key and self.user_id is not None:
            raise ValueError("legacy API-key access cannot carry a user id")
        if not self.legacy_api_key and self.user_id is None:
            raise ValueError("user conversation access requires a user id")

    @classmethod
    def for_user(cls, user_id: uuid.UUID) -> "ConversationAccess":
        return cls(user_id=user_id, legacy_api_key=False)

    @classmethod
    def for_legacy_api_key(cls) -> "ConversationAccess":
        return cls(user_id=None, legacy_api_key=True)


@dataclass(frozen=True)
class AuthorizedConversation:
    """A session and dataset already checked against a trusted actor."""

    session: ConversationSession
    dataset: DatasetRecord


# ── sessions ────────────────────────────────────────────────────────────────

async def create_session_for_access(
    *,
    session_db: AsyncSession,
    dataset_id: uuid.UUID,
    mode: ConversationMode = ConversationMode.AUTO,
    access: ConversationAccess,
) -> ConversationSession:
    """Create a session only after authorizing the dataset for ``access``."""
    record = await get_dataset_for_access(session_db, dataset_id, access=access)
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"dataset {dataset_id} is not ready for chat",
            details={"status": record.status.value},
        )
    return await create_session(
        session_db=session_db,
        dataset_id=dataset_id,
        mode=mode,
        user_id=None if access.legacy_api_key else access.user_id,
    )


async def get_dataset_for_access(
    session_db: AsyncSession,
    dataset_id: uuid.UUID,
    *,
    access: ConversationAccess,
) -> DatasetRecord:
    """Return a dataset visible to this conversation actor or a non-enumerating 404."""
    record = await session_db.get(DatasetRecord, dataset_id)
    if record is None or not _can_access_dataset(record, access):
        raise NotFoundError(
            f"dataset {dataset_id} not found",
            details={"dataset_id": str(dataset_id)},
        )
    return record


async def get_session_for_access(
    session_db: AsyncSession,
    session_id: uuid.UUID,
    *,
    access: ConversationAccess,
) -> AuthorizedConversation:
    """Load a session and its dataset after enforcing ownership centrally."""
    row, dataset = await _get_session_row_for_access(
        session_db, session_id, access=access
    )
    return AuthorizedConversation(session=_row_to_session(row), dataset=dataset)

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
    """Fetch a session for trusted internal orchestration only.

    HTTP handlers must use :func:`get_session_for_access` instead.
    """
    row = await _get_session_row(session_db, session_id)
    return _row_to_session(row)


async def list_sessions_for_dataset(
    session_db: AsyncSession,
    dataset_id: uuid.UUID,
    *,
    limit: int = 50,
    access: ConversationAccess,
) -> list[ConversationSession]:
    """List only sessions visible to ``access``, most-recently-active first."""
    dataset = await get_dataset_for_access(session_db, dataset_id, access=access)
    stmt = (
        select(ConversationSessionRow)
        .where(ConversationSessionRow.dataset_id == dataset_id)
        .order_by(ConversationSessionRow.last_active_at.desc())
        .limit(limit)
    )
    if access.legacy_api_key:
        stmt = stmt.where(ConversationSessionRow.user_id.is_(None))
    else:
        stmt = stmt.where(
            or_(
                ConversationSessionRow.user_id == access.user_id,
                ConversationSessionRow.user_id.is_(None),
            )
        )
    result = await session_db.execute(stmt)
    return [
        _row_to_session(row)
        for row in result.scalars().all()
        if _can_access_conversation(row, dataset, access)
    ]


async def delete_session(
    session_db: AsyncSession,
    session_id: uuid.UUID,
    *,
    access: ConversationAccess,
) -> None:
    """Hard-delete an actor-authorized session and its messages."""
    row, _dataset = await _get_session_row_for_access(
        session_db, session_id, access=access
    )
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
    unsupported: bool = False,
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
        unsupported=unsupported,
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


async def get_recent_messages_for_access(
    session_db: AsyncSession,
    session_id: uuid.UUID,
    *,
    access: ConversationAccess,
    limit: int = MAX_HISTORY_MESSAGES,
) -> tuple[AuthorizedConversation, list[ChatMessage]]:
    """Read a transcript only after resolving the shared ownership boundary."""
    conversation = await get_session_for_access(
        session_db, session_id, access=access
    )
    messages = await get_recent_messages(session_db, session_id, limit=limit)
    return conversation, messages


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


async def _get_session_row_for_access(
    session_db: AsyncSession,
    session_id: uuid.UUID,
    *,
    access: ConversationAccess,
) -> tuple[ConversationSessionRow, DatasetRecord]:
    """Resolve the session/dataset pair without disclosing ownership mismatches."""
    row = await session_db.get(ConversationSessionRow, session_id)
    if row is None:
        _raise_session_not_found(session_id)

    dataset = await session_db.get(DatasetRecord, row.dataset_id)
    if (
        dataset is None
        or not _can_access_dataset(dataset, access)
        or not _can_access_conversation(row, dataset, access)
    ):
        _raise_session_not_found(session_id)
    return row, dataset


def _can_access_dataset(
    dataset: DatasetRecord, access: ConversationAccess
) -> bool:
    if access.legacy_api_key:
        return dataset.user_id is None
    return dataset.user_id == access.user_id


def _can_access_conversation(
    conversation: ConversationSessionRow,
    dataset: DatasetRecord,
    access: ConversationAccess,
) -> bool:
    """Enforce a single ownership policy for all session operations.

    JWT callers must own the linked dataset and either own the session or be
    reclaiming a historical null-owned session attached to their dataset.
    Legacy API-key callers can use only completely legacy (null-owned) pairs.
    """
    if not _can_access_dataset(dataset, access):
        return False
    if access.legacy_api_key:
        return conversation.user_id is None
    return conversation.user_id == access.user_id or conversation.user_id is None


def _raise_session_not_found(session_id: uuid.UUID) -> None:
    raise NotFoundError(
        f"conversation session {session_id} not found",
        details={"session_id": str(session_id)},
    )


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
        # ``expression`` is the exact normalised string that the query
        # executor validated and compiled. It remains display-only: prompt
        # history is built solely from ``content`` above.
        generated_code=(query_result.expression or None) if query_result else None,
        token_usage=token_usage,
        degraded=row.degraded,
        degraded_reason=row.degraded_reason,
        unsupported=row.unsupported,
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
