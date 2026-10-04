"""Persistence layer for chat sessions and messages.

Thin async CRUD on top of the SQLAlchemy ORM models in
:mod:`app.db.models.conversation`. The chat agent uses this module as
its only persistence touchpoint — it never talks to the ORM directly,
which keeps the orchestrator free of session-handling boilerplate.

Three responsibilities:

  * Create / fetch / list / delete :class:`ConversationSession`s bound to an
    uploaded dataset or a creator-owned PostgreSQL source.
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
from app.db.models.source import DataSource
from app.exceptions.custom_exceptions import NotFoundError
from app.services.postgres_query_executor import PostgresQueryExecutionResult
from agentic_engine.conversational.models import (
    MAX_HISTORY_MESSAGES,
    ChatMessage,
    ChatRole,
    ConversationMode,
    ConversationSession,
    IntentDecision,
    PostgresChatResult,
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
    """A session and its one authorized backing resource.

    Uploaded-file chat sessions retain ``dataset``; PostgreSQL chat sessions
    have ``source`` instead.  The invariant is checked here as a second
    in-process boundary in addition to the persistence-level XOR check.
    """

    session: ConversationSession
    dataset: DatasetRecord | None = None
    source: DataSource | None = None

    def __post_init__(self) -> None:
        if (self.dataset is None) == (self.source is None):
            raise ValueError("authorized conversation must have exactly one resource")
        if self.dataset is not None:
            if (
                self.session.dataset_id != self.dataset.id
                or self.session.source_id is not None
            ):
                raise ValueError("authorized conversation dataset binding does not match session")
        elif self.source is not None and (
            self.session.source_id != self.source.id
            or self.session.dataset_id is not None
        ):
            raise ValueError("authorized conversation source binding does not match session")


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


async def get_source_for_access(
    session_db: AsyncSession,
    source_id: uuid.UUID,
    *,
    access: ConversationAccess,
) -> DataSource:
    """Return a creator-owned source visible to a JWT conversation actor.

    Sources have no legacy/null-owner namespace.  Rejecting a valid legacy
    API key here (rather than relying on a route convention) makes the
    JWT-only database-chat boundary central and reusable by every lifecycle
    operation.
    """
    if access.legacy_api_key:
        _raise_source_not_found(source_id)

    record = await session_db.get(DataSource, source_id)
    if record is None or record.owner_user_id != access.user_id:
        _raise_source_not_found(source_id)
    return record


async def get_session_for_access(
    session_db: AsyncSession,
    session_id: uuid.UUID,
    *,
    access: ConversationAccess,
) -> AuthorizedConversation:
    """Load a session and its one backing resource after central authorization."""
    row, dataset, source = await _get_session_row_for_access(
        session_db, session_id, access=access
    )
    return AuthorizedConversation(
        session=_row_to_session(row), dataset=dataset, source=source
    )


async def create_database_session_for_access(
    *,
    session_db: AsyncSession,
    source_id: uuid.UUID,
    mode: ConversationMode = ConversationMode.AUTO,
    access: ConversationAccess,
) -> ConversationSession:
    """Create a JWT-only session bound to one currently owner-authorized source.

    Readiness of a catalog snapshot is deliberately not inferred from source
    connection status here.  The Stage 4C planner/executor resolves the
    current authorized snapshot for every turn; this function only establishes
    the durable ownership binding.
    """
    source = await get_source_for_access(session_db, source_id, access=access)
    # ``get_source_for_access`` rejects legacy actors and only returns a
    # source for a non-null JWT owner.  Keep this assertion explicit so a
    # future change cannot create a null-owned source transcript.
    if access.user_id is None:  # pragma: no cover - guarded by ConversationAccess
        _raise_source_not_found(source_id)
    return await create_database_session(
        session_db=session_db,
        source_id=source.id,
        mode=mode,
        user_id=access.user_id,
    )

async def create_session(
    *,
    session_db: AsyncSession,
    dataset_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    mode: ConversationMode = ConversationMode.AUTO,
    user_id: uuid.UUID | None = None,
) -> ConversationSession:
    """Create a new conversation session for exactly one resource.

    Commits on success so the caller can immediately hand the new
    session id back to the client.
    """
    _require_exactly_one_binding(dataset_id=dataset_id, source_id=source_id)
    row = ConversationSessionRow(
        dataset_id=dataset_id,
        source_id=source_id,
        mode=ConversationModeDB(mode.value),
        user_id=user_id,
    )
    session_db.add(row)
    await session_db.commit()
    await session_db.refresh(row)

    logger.info(
        "conversation.session_created",
        session_id=str(row.id),
        dataset_id=str(dataset_id) if dataset_id is not None else None,
        source_id=str(source_id) if source_id is not None else None,
        mode=mode.value,
    )
    return _row_to_session(row)


async def create_database_session(
    *,
    session_db: AsyncSession,
    source_id: uuid.UUID,
    mode: ConversationMode = ConversationMode.AUTO,
    user_id: uuid.UUID,
) -> ConversationSession:
    """Create a source-bound session for trusted internal orchestration.

    HTTP paths must use :func:`create_database_session_for_access` so source
    ownership is checked before this trusted primitive is reached.
    """
    return await create_session(
        session_db=session_db,
        source_id=source_id,
        mode=mode,
        user_id=user_id,
    )


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


async def list_sessions_for_source(
    session_db: AsyncSession,
    source_id: uuid.UUID,
    *,
    limit: int = 50,
    access: ConversationAccess,
) -> list[ConversationSession]:
    """List source-bound sessions owned by the current JWT source creator.

    The initial source authorization is intentionally performed before the
    session query.  That keeps an unknown source and a foreign-owned source
    indistinguishable, and it prevents the legacy API-key namespace from
    observing database-chat history.
    """
    source = await get_source_for_access(session_db, source_id, access=access)
    stmt = (
        select(ConversationSessionRow)
        .where(ConversationSessionRow.source_id == source.id)
        .where(ConversationSessionRow.user_id == access.user_id)
        .order_by(ConversationSessionRow.last_active_at.desc())
        .limit(limit)
    )
    result = await session_db.execute(stmt)
    return [
        _row_to_session(row)
        for row in result.scalars().all()
        if _can_access_source_conversation(row, source, access)
    ]


async def delete_session(
    session_db: AsyncSession,
    session_id: uuid.UUID,
    *,
    access: ConversationAccess,
) -> None:
    """Hard-delete an actor-authorized session and its messages."""
    row, _dataset, _source = await _get_session_row_for_access(
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
    postgres_result: PostgresChatResult | PostgresQueryExecutionResult | None = None,
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
    normalized_postgres_result = _normalize_postgres_result(postgres_result)

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
        postgres_result=(
            normalized_postgres_result.model_dump(mode="json")
            if normalized_postgres_result is not None
            else None
        ),
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
    if row is None or not _has_exactly_one_binding(row):
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
) -> tuple[ConversationSessionRow, DatasetRecord | None, DataSource | None]:
    """Resolve the one session resource without disclosing ownership mismatches.

    A session is always re-resolved through its backing resource.  In
    particular, a source-bound row whose source disappeared is indistinguish-
    able from an unknown session even if an incomplete migration or a disabled
    database foreign-key setting left the row behind.
    """
    row = await session_db.get(ConversationSessionRow, session_id)
    if row is None or not _has_exactly_one_binding(row):
        _raise_session_not_found(session_id)

    if row.dataset_id is not None:
        dataset = await session_db.get(DatasetRecord, row.dataset_id)
        if (
            dataset is None
            or not _can_access_dataset(dataset, access)
            or not _can_access_conversation(row, dataset, access)
        ):
            _raise_session_not_found(session_id)
        return row, dataset, None

    # The XOR guard above proves this is non-null, but retain a local guard so
    # type checkers and future schema changes cannot accidentally turn it into
    # an unscoped lookup.
    if row.source_id is None:  # pragma: no cover - guarded by XOR above
        _raise_session_not_found(session_id)
    source = await session_db.get(DataSource, row.source_id)
    if source is None or not _can_access_source_conversation(row, source, access):
        _raise_session_not_found(session_id)
    return row, None, source


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


def _can_access_source_conversation(
    conversation: ConversationSessionRow,
    source: DataSource,
    access: ConversationAccess,
) -> bool:
    """Return whether a JWT owner may use a source-bound session.

    Unlike dataset sessions, source sessions cannot be reclaimed from a legacy
    null-owner namespace.  Requiring all three identities to agree prevents a
    historical message row from being served after source access changes.
    """
    return (
        not access.legacy_api_key
        and access.user_id is not None
        and conversation.source_id == source.id
        and source.owner_user_id == access.user_id
        and conversation.user_id == access.user_id
    )


def _has_exactly_one_binding(row: ConversationSessionRow) -> bool:
    return (row.dataset_id is None) != (row.source_id is None)


def _require_exactly_one_binding(
    *, dataset_id: uuid.UUID | None, source_id: uuid.UUID | None
) -> None:
    if (dataset_id is None) == (source_id is None):
        raise ValueError("conversation session must bind exactly one dataset or source")


def _raise_session_not_found(session_id: uuid.UUID) -> None:
    raise NotFoundError(
        f"conversation session {session_id} not found",
        details={"session_id": str(session_id)},
    )


def _raise_source_not_found(source_id: uuid.UUID) -> None:
    """Keep unknown and foreign source authorization failures non-enumerating."""
    raise NotFoundError(
        f"source {source_id} not found",
        details={"source_id": str(source_id)},
    )


def _row_to_session(row: ConversationSessionRow) -> ConversationSession:
    return ConversationSession(
        session_id=row.id,
        dataset_id=row.dataset_id,
        source_id=row.source_id,
        mode=ConversationMode(row.mode.value),
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        message_count=row.message_count,
    )


def _row_to_message(row: ChatMessageRow) -> ChatMessage:
    intent = _decode_intent(row.intent)
    citations = _decode_citations(row.citations)
    query_result = _decode_query_result(row.query_result)
    postgres_result = _decode_postgres_result(row.postgres_result)
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
        postgres_result=postgres_result,
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


def _normalize_postgres_result(
    result: PostgresChatResult | PostgresQueryExecutionResult | None,
) -> PostgresChatResult | None:
    """Normalize the executor's typed outcome before persistence.

    The public executor intentionally returns ``PostgresQueryExecutionResult``
    while chat exposes its identically shaped ``PostgresChatResult``.  Dumping
    then validating keeps the boundary explicit and means arbitrary mappings
    cannot be slipped into the transcript blob.
    """
    if result is None:
        return None
    if isinstance(result, PostgresChatResult):
        return result
    return PostgresChatResult.model_validate(result.model_dump(mode="python"))


def _decode_postgres_result(blob: dict[str, Any] | None) -> PostgresChatResult | None:
    """Decode only the strict Stage 4B result shape without logging row values."""
    if not blob:
        return None
    try:
        return PostgresChatResult.model_validate(blob)
    except Exception:  # pragma: no cover - schema drift / corrupt-row guard
        # Validation errors can echo an invalid value (including a result cell),
        # so deliberately avoid serializing the exception into audit logs.
        logger.warning("conversation.postgres_result_decode_failed")
        return None


def _decode_token_usage(
    input_tokens: int | None, output_tokens: int | None
) -> TokenUsage | None:
    if input_tokens is None and output_tokens is None:
        return None
    usage = TokenUsage()
    usage.add(input_tokens or 0, output_tokens or 0)
    return usage
