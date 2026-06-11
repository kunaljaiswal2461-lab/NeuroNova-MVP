"""Conversational chat routes — Layer 7 HTTP surface.

Five endpoints, all under ``/api/v1`` and ``X-API-Key`` protected:

  * ``POST   /datasets/{dataset_id}/chat/sessions``      — create a session
  * ``GET    /datasets/{dataset_id}/chat/sessions``      — list sessions
  * ``GET    /chat/sessions/{session_id}``               — fetch a session
  * ``DELETE /chat/sessions/{session_id}``               — delete a session + history
  * ``GET    /chat/sessions/{session_id}/messages``      — read transcript
  * ``POST   /chat/sessions/{session_id}/message``       — streaming chat turn (SSE)

The streaming turn is implemented as a Server-Sent Events response:
each :class:`ChatStreamEvent` from the chat agent is encoded as one
``event: <type>\\ndata: <json>\\n\\n`` frame. The frontend's
``EventSource`` reads them in order and renders incrementally.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, status
from fastapi.responses import Response, StreamingResponse

from fastapi import Depends
from app.core.dependencies import AuthContext, DBSession, SettingsDep, get_auth_context
from app.core.logging import get_logger
from app.db.models.dataset import DatasetStatus
from app.exceptions.custom_exceptions import NotFoundError
from app.schemas.chat_schemas import (
    CreateSessionRequest,
    MessageListResponse,
    SendMessageRequest,
    SessionListResponse,
    SessionResponse,
)
from app.services import dataset_service
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.chat_agent import stream_chat_turn
from agentic_engine.conversational.models import ChatStreamEvent


logger = get_logger("chat_routes")


router = APIRouter(
    prefix="/api/v1",
    tags=["chat"],
    dependencies=[AuthContext],  # accepts JWT Bearer or legacy X-API-Key
)


# ── session lifecycle ───────────────────────────────────────────────────────

@router.post(
    "/datasets/{dataset_id}/chat/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a chat session against a dataset",
)
async def create_session(
    dataset_id: uuid.UUID,
    body: CreateSessionRequest,
    session: DBSession,
    current_user=Depends(get_auth_context),
) -> SessionResponse:
    # Sessions can only be opened on datasets that have completed the
    # full pipeline — the chat agent depends on the profile, findings,
    # and (for RAG) the vector index all being on disk / in PG.
    record = await dataset_service.get_dataset(session, dataset_id)
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"dataset {dataset_id} is not ready for chat",
            details={"status": record.status.value},
        )
    created = await manager.create_session(
        session_db=session,
        dataset_id=dataset_id,
        mode=body.mode,
        user_id=current_user.id if current_user is not None else None,
    )
    return SessionResponse(session=created)


@router.get(
    "/datasets/{dataset_id}/chat/sessions",
    response_model=SessionListResponse,
    summary="List chat sessions for a dataset (most recent first)",
)
async def list_sessions(
    dataset_id: uuid.UUID,
    session: DBSession,
    limit: int = Query(50, ge=1, le=200),
) -> SessionListResponse:
    sessions = await manager.list_sessions_for_dataset(
        session, dataset_id, limit=limit
    )
    return SessionListResponse(sessions=sessions, count=len(sessions))


@router.get(
    "/chat/sessions/{session_id}",
    response_model=SessionResponse,
    summary="Fetch a single chat session",
)
async def get_session(
    session_id: uuid.UUID,
    session: DBSession,
) -> SessionResponse:
    fetched = await manager.get_session(session, session_id)
    return SessionResponse(session=fetched)


@router.delete(
    "/chat/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a chat session and all its messages",
)
async def delete_session(
    session_id: uuid.UUID,
    session: DBSession,
) -> None:
    await manager.delete_session(session, session_id)


# ── transcript ──────────────────────────────────────────────────────────────

@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=MessageListResponse,
    summary="Read the conversation transcript",
)
async def list_messages(
    session_id: uuid.UUID,
    session: DBSession,
    limit: int = Query(100, ge=1, le=500),
) -> MessageListResponse:
    # Ensure the session exists so callers get a 404 (not an empty list)
    # when they hit a stale id.
    await manager.get_session(session, session_id)
    messages = await manager.get_recent_messages(session, session_id, limit=limit)
    return MessageListResponse(
        session_id=session_id,
        messages=messages,
        count=len(messages),
    )


# ── streaming chat turn (SSE) ───────────────────────────────────────────────

@router.post(
    "/chat/sessions/{session_id}/message",
    summary="Send a user message and stream the assistant reply (SSE)",
    responses={
        200: {
            "description": (
                "Server-Sent Events stream. Each frame is "
                "`event: <type>\\ndata: <json>\\n\\n` where `<type>` is one "
                "of start / intent / citations / query_result / token / done / error."
            ),
            "content": {"text/event-stream": {}},
        },
    },
)
async def send_message(
    session_id: uuid.UUID,
    body: SendMessageRequest,
    session: DBSession,
    settings: SettingsDep,
) -> StreamingResponse:
    # Probe the session up-front so we can return a 404 cleanly before
    # opening the SSE stream — clients much prefer a status code over a
    # malformed event stream.
    await manager.get_session(session, session_id)

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            async for event in stream_chat_turn(
                session_id=session_id,
                user_message=body.message,
                session_db=session,
                settings=settings,
            ):
                yield _encode_sse(event)
        except Exception as exc:  # pragma: no cover — top-level safety
            # If the agent itself crashes (it tries hard not to), emit
            # one terminal error frame so the client closes cleanly
            # rather than hanging on a dead connection.
            logger.exception(
                "chat_routes.stream_crashed",
                session_id=str(session_id),
                error=str(exc),
            )
            yield _encode_sse_raw(
                event="error",
                data={"message": "internal error in chat stream"},
            )
            yield _encode_sse_raw(event="done", data={})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # Tell reverse proxies (nginx) not to buffer the stream so
            # the client sees tokens as they are produced.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── SSE encoding ────────────────────────────────────────────────────────────

def _encode_sse(event: ChatStreamEvent) -> bytes:
    """Encode one :class:`ChatStreamEvent` as a single SSE frame."""
    return _encode_sse_raw(event=event.event.value, data=event.data)


def _encode_sse_raw(*, event: str, data: dict) -> bytes:
    """Low-level SSE frame builder.

    We use the ``event:`` field even though most clients also work
    without it; having a typed event name makes client-side dispatch
    (``source.addEventListener("token", ...)``) trivial.
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
