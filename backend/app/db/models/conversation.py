"""SQLAlchemy models for the conversational layer (Layer 7).

Two tables — sessions and messages — joined via FK with cascade delete.

  * ``conversation_sessions`` — one row per chat session, scoped to a
    dataset. Stores the routing mode the session was created with so an
    explicit pin (RAG-only / QUERY-only) survives restarts.
  * ``chat_messages`` — one row per turn (user or assistant). Assistant
    rows carry the routing decision, the RAG citations, and the
    NL-query result as JSONB blobs so we never need to join across more
    tables to render a transcript.

We deliberately store citations and query results as JSONB rather than
as relational rows. They are *artefacts of a turn*, not first-class
entities, and storing them inline matches the natural read pattern
("hydrate one message").
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


# ── enums ────────────────────────────────────────────────────────────────────

class ConversationModeDB(str, enum.Enum):
    """Mirror of :class:`agentic_engine.conversational.models.ConversationMode`.

    Kept as a separate enum so the database schema is decoupled from
    application enum changes — adding a new conversational mode requires
    an explicit migration, not just a Python-side enum addition.
    """

    AUTO = "AUTO"
    RAG = "RAG"
    QUERY = "QUERY"


class ChatRoleDB(str, enum.Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


# ── tables ───────────────────────────────────────────────────────────────────

class ConversationSessionRow(Base, TimestampMixin):
    """Header row for one conversation. Owned by exactly one dataset."""

    __tablename__ = "conversation_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )

    # Cascade delete tied to the dataset: blowing away a dataset removes
    # all its conversations and (via the messages FK below) all their
    # messages in a single statement.
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    mode: Mapped[ConversationModeDB] = mapped_column(
        Enum(ConversationModeDB, name="conversation_mode_enum", native_enum=True),
        nullable=False,
        default=ConversationModeDB.AUTO,
    )

    # Touched on every appended message so the UI can sort recent
    # sessions to the top without scanning the messages table.
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    messages: Mapped[list["ChatMessageRow"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ChatMessageRow(Base):
    """One persisted chat turn — user or assistant.

    User rows fill only ``role`` + ``content``. Assistant rows
    additionally carry ``mode_used`` (which branch handled the turn),
    ``intent`` (the routing decision), ``citations`` (RAG-mode only),
    ``query_result`` (QUERY-mode only), and token usage / degradation
    metadata.
    """

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session: Mapped[ConversationSessionRow] = relationship(
        back_populates="messages"
    )

    role: Mapped[ChatRoleDB] = mapped_column(
        Enum(ChatRoleDB, name="chat_role_enum", native_enum=True),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Only populated on ASSISTANT rows. Stored as a plain string column
    # rather than a second enum so we don't fork the
    # ``conversation_mode_enum`` (this column also needs to express
    # which branch was chosen *for this turn*, which AUTO never can —
    # it'd always be RAG or QUERY in practice on the assistant side).
    mode_used: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    # Per-turn artefacts. JSONB so we can query for "all turns that
    # cited finding X" or "all turns that ran a Polars expression" in
    # the future without schema work.
    intent: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    query_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    degraded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    degraded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Token counts denormalised onto the row (instead of a nested JSON
    # under ``intent``) so per-session cost roll-ups are a single SQL
    # ``SUM`` rather than a JSON traversal.
    token_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_output: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
