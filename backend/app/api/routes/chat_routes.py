"""Conversational chat routes — Layer 7 HTTP surface.

Endpoints are available to JWT users and to the tightly scoped legacy API-key
namespace. A legacy key can never act as an arbitrary user's session owner.

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
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from fastapi import Depends
from app.core.config import get_settings
from app.core.dependencies import AuthContext, DBSession, SettingsDep, get_auth_context
from app.middlewares.rate_limiter import limiter
from app.core.logging import get_logger
from app.schemas.chat_schemas import (
    CreateSessionRequest,
    MessageListResponse,
    SendMessageRequest,
    SessionListResponse,
    SessionResponse,
)
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.chat_agent import stream_chat_turn
from agentic_engine.conversational.models import ChatStreamEvent


logger = get_logger("chat_routes")


async def get_conversation_access(
    current_user=Depends(get_auth_context),
) -> manager.ConversationAccess:
    """Build an actor only from the already-verified authentication context.

    ``get_auth_context`` returns ``None`` exclusively for a valid legacy
    ``X-API-Key``. The manager receives an explicit legacy actor rather than a
    bare ``None`` user id, preventing the legacy all-datasets behavior used by
    older dataset endpoints from leaking into conversation authorization.
    """
    if current_user is None:
        return manager.ConversationAccess.for_legacy_api_key()
    return manager.ConversationAccess.for_user(current_user.id)


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
@limiter.limit(get_settings().rate_limit_session_create)
async def create_session(
    request: Request,
    dataset_id: uuid.UUID,
    body: CreateSessionRequest,
    session: DBSession,
    access: manager.ConversationAccess = Depends(get_conversation_access),
) -> SessionResponse:
    created = await manager.create_session_for_access(
        session_db=session,
        dataset_id=dataset_id,
        mode=body.mode,
        access=access,
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
    access: manager.ConversationAccess = Depends(get_conversation_access),
) -> SessionListResponse:
    sessions = await manager.list_sessions_for_dataset(
        session, dataset_id, limit=limit, access=access
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
    access: manager.ConversationAccess = Depends(get_conversation_access),
) -> SessionResponse:
    conversation = await manager.get_session_for_access(
        session, session_id, access=access
    )
    return SessionResponse(session=conversation.session)


@router.delete(
    "/chat/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a chat session and all its messages",
)
async def delete_session(
    session_id: uuid.UUID,
    session: DBSession,
    access: manager.ConversationAccess = Depends(get_conversation_access),
) -> None:
    await manager.delete_session(session, session_id, access=access)


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
    access: manager.ConversationAccess = Depends(get_conversation_access),
) -> MessageListResponse:
    _conversation, messages = await manager.get_recent_messages_for_access(
        session, session_id, access=access, limit=limit
    )
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
@limiter.limit(get_settings().rate_limit_chat_message)
async def send_message(
    request: Request,
    session_id: uuid.UUID,
    body: SendMessageRequest,
    session: DBSession,
    settings: SettingsDep,
    access: manager.ConversationAccess = Depends(get_conversation_access),
) -> StreamingResponse:
    # Resolve ownership before constructing the stream. This prevents an
    # unauthorized caller from reaching model calls, file-query execution, or
    # the generator that writes messages to the transcript.
    conversation = await manager.get_session_for_access(
        session, session_id, access=access
    )

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            async for event in stream_chat_turn(
                session_id=session_id,
                user_message=body.message,
                session_db=session,
                settings=settings,
                authorized_session=conversation.session,
                authorized_dataset=conversation.dataset,
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
