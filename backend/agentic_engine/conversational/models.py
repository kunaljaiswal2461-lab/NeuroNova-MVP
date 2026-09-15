"""Pydantic models for Layer 7 — conversational chat.

Everything the chat agent, query executor, RAG answerer, conversation
manager, and HTTP route layer all share lives here. These are the
on-the-wire shapes; persistence-layer ORM rows are kept separately in
``app/db/models/conversation.py`` and converted at the manager boundary.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.llm_engine.models import TokenUsage


# ── enums ────────────────────────────────────────────────────────────────────

class ChatRole(str, enum.Enum):
    """Role of a single :class:`ChatMessage`."""

    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ConversationMode(str, enum.Enum):
    """Session-level routing preference.

      * AUTO — let the intent classifier pick per message (default).
      * RAG — force every turn through the finding-retrieval path.
      * QUERY — force every turn through the NL→Polars path.

    The session mode acts as an override. When set to anything other
    than AUTO, the chat agent skips the classifier entirely and routes
    every message through that branch.
    """

    AUTO = "AUTO"
    RAG = "RAG"
    QUERY = "QUERY"


class IntentLabel(str, enum.Enum):
    """Output of the intent classifier for a single user message.

    ``AMBIGUOUS`` is an *internal* state — the heuristic was unable to
    pick a side and the LLM fallback will be invoked. It is never
    persisted on a :class:`ChatMessage`.
    """

    RAG = "RAG"
    QUERY = "QUERY"
    AMBIGUOUS = "AMBIGUOUS"


class IntentRoutedBy(str, enum.Enum):
    """Provenance of an intent decision (for observability / UI)."""

    HEURISTIC = "heuristic"
    LLM = "llm"
    SESSION_OVERRIDE = "session_override"


# ── intent ──────────────────────────────────────────────────────────────────

class IntentDecision(BaseModel):
    """Final intent decision for a single user message."""

    mode: Literal["RAG", "QUERY"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    routed_by: IntentRoutedBy


# ── RAG ──────────────────────────────────────────────────────────────────────

class RagCitation(BaseModel):
    """One citation backing a RAG answer — surfaced to the UI for
    "show me the evidence" affordances."""

    finding_id: uuid.UUID
    similarity: float = Field(ge=0.0, le=1.0)
    title: str
    severity: Severity
    finding_type: FindingType
    column: str | None = None


# ── NL→Polars query ──────────────────────────────────────────────────────────

class QueryColumn(BaseModel):
    """One column descriptor for a :class:`QueryResult`."""

    name: str
    dtype: str


class QueryResult(BaseModel):
    """Outcome of a single NL→Polars query execution.

    ``rows`` is a JSON-safe list of cell values (already coerced from
    Polars dtypes via ``DataFrame.rows()``). ``truncated`` is True when
    the result was capped at :data:`QUERY_ROW_LIMIT` rows so the UI can
    surface a "showing first N" hint.

    On rejection or runtime failure the executor still returns a
    structurally valid :class:`QueryResult` with ``rows=[]`` and the
    failure reason in ``error`` — never raises into the chat agent.
    """

    expression: str = Field(description="The Polars expression that was executed")
    columns: list[QueryColumn] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = Field(ge=0, default=0)
    truncated: bool = False
    elapsed_ms: float = Field(ge=0.0, default=0.0)
    error: str | None = None
    # Provenance for the executed expression: how many codegen attempts
    # were needed (1 = first try, 2 = repaired after a failure) and which
    # legacy-API renames the compatibility layer applied, if any.
    attempts: int = Field(ge=1, default=1)
    rewrites: list[str] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=False)

    @property
    def is_ok(self) -> bool:
        return self.error is None


# ── chat message + session ───────────────────────────────────────────────────

class ChatMessage(BaseModel):
    """One persisted turn — either a user message or an assistant reply.

    Assistant messages carry every artefact the UI may want to render:
    the routing decision, the RAG citations (if any), the query result
    (if any), and per-turn token usage for cost reporting.
    """

    message_id: uuid.UUID
    session_id: uuid.UUID
    role: ChatRole
    content: str
    created_at: datetime

    # Only populated on ASSISTANT messages — None for USER messages.
    mode_used: ConversationMode | None = None
    intent: IntentDecision | None = None
    citations: list[RagCitation] = Field(default_factory=list)
    query_result: QueryResult | None = None
    # Display-only provenance for QUERY turns. This is deliberately kept out
    # of ``history_for_prompt`` so generated query code is never sent back to
    # the LLM on later turns.
    generated_code: str | None = None
    # True when QUERY mode reached the deliberate capability boundary rather
    # than attempting a free-form-code fallback.
    unsupported: bool = False
    token_usage: TokenUsage | None = None

    # True when the turn completed but in a degraded mode (e.g. retriever
    # was unavailable so we answered from raw history with no grounding).
    degraded: bool = False
    degraded_reason: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ConversationSession(BaseModel):
    """Top-level session record. One per (dataset, user-flow)."""

    session_id: uuid.UUID
    dataset_id: uuid.UUID
    mode: ConversationMode = ConversationMode.AUTO
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_active_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    message_count: int = 0

    model_config = ConfigDict(from_attributes=True)


# ── stream events (SSE wire format) ─────────────────────────────────────────

class ChatStreamEventType(str, enum.Enum):
    """Wire-level event types emitted by the streaming chat endpoint.

    The UI listens for ``token`` for incremental rendering and treats
    ``done`` as the signal to capture the final message_id + usage.
    ``citations`` and ``query_result`` arrive *before* the first token
    so the UI can place chrome (citation chips, table preview) above the
    streaming text.
    """

    START = "start"
    INTENT = "intent"
    CITATIONS = "citations"
    QUERY_RESULT = "query_result"
    TOKEN = "token"
    DONE = "done"
    ERROR = "error"


class ChatStreamEvent(BaseModel):
    """One typed event in a chat SSE stream.

    The ``data`` payload is loosely typed because each event type has a
    different shape — strong typing would mean a discriminated union per
    type and would not buy enough over a JSON object here. The agent
    builds each event through a typed helper, so the call sites stay
    safe.
    """

    event: ChatStreamEventType
    data: dict[str, Any] = Field(default_factory=dict)


# ── module-wide constants ────────────────────────────────────────────────────

# Rolling history window for a conversation. 30 messages keeps the LLM
# context bounded (≈15 user turns) and matches the MVP product brief.
MAX_HISTORY_MESSAGES: int = 30

# Maximum rows returned to the user from a single NL→Polars query. Above
# this the executor truncates and flags ``truncated=True``. The cap is
# both a UI sanity bound and a memory bound for the SSE response.
QUERY_ROW_LIMIT: int = 200

# Per-query wall-clock budget for the executor. The expression runs in a
# worker thread; if it exceeds this we surface an error rather than let
# a runaway computation tie up the event loop.
QUERY_TIMEOUT_SECONDS: float = 5.0
