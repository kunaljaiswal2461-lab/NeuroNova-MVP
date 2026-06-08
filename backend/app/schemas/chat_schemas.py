"""Pydantic request/response schemas for the chat HTTP surface.

These are the on-the-wire shapes. They re-use the domain models from
``agentic_engine.conversational.models`` for response payloads so the
chat agent does not need a second round of serialisation; we only
introduce extra types where the HTTP request shape differs from the
internal shape.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from agentic_engine.conversational.models import (
    ChatMessage,
    ConversationMode,
    ConversationSession,
)


# ── requests ────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    """Body for ``POST /datasets/{dataset_id}/chat/sessions``."""

    mode: ConversationMode = Field(
        default=ConversationMode.AUTO,
        description=(
            "AUTO routes per-message via the intent classifier; "
            "RAG / QUERY pin every turn to that branch."
        ),
    )


class SendMessageRequest(BaseModel):
    """Body for ``POST /chat/sessions/{session_id}/message`` (SSE)."""

    message: str = Field(
        min_length=1,
        max_length=4000,
        description="The user's natural-language message for this turn.",
    )


# ── responses ───────────────────────────────────────────────────────────────

class SessionResponse(BaseModel):
    """Returned by session create / get endpoints."""

    model_config = ConfigDict(from_attributes=True)

    session: ConversationSession


class SessionListResponse(BaseModel):
    """Returned by ``GET /datasets/{dataset_id}/chat/sessions``."""

    sessions: list[ConversationSession]
    count: int


class MessageListResponse(BaseModel):
    """Returned by ``GET /chat/sessions/{session_id}/messages``."""

    session_id: uuid.UUID
    messages: list[ChatMessage]
    count: int
