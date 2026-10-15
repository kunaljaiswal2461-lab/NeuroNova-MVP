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
import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from fastapi import Depends
from app.core.config import get_settings
from app.core.dependencies import (
    AuthContext,
    DBSession,
    SettingsDep,
    get_auth_context,
    get_current_user,
)
from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.db.models.user import User
from app.exceptions.custom_exceptions import AppError, NotFoundError
from app.middlewares.rate_limiter import limiter
from app.core.logging import get_logger
from app.services.nl_sql import messages as nl_messages
from app.schemas.chat_schemas import (
    CreateSessionRequest,
    MessageListResponse,
    SendMessageRequest,
    SessionListResponse,
    SessionResponse,
)
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.chat_agent import stream_chat_turn
from agentic_engine.conversational.postgres_chat_agent import stream_postgres_chat_turn
from agentic_engine.conversational.models import ChatStreamEvent, ConversationMode


logger = get_logger("chat_routes")


class TurnInProgressError(AppError):
    """A second database turn was posted on a session that already has one in flight (SPEC-09 W2.2 step 5)."""

    status_code = 409
    code = "turn_in_progress"

    def __init__(self) -> None:
        super().__init__("A database turn is already in progress for this conversation.")


# A registered turn older than this is treated as abandoned (a stream that never started cannot clean up after
# itself); no real turn lives this long because every statement and model call is time-bounded.
STALE_TURN_SECONDS = 15 * 60


@dataclass(frozen=True)
class _ActiveDatabaseTurn:
    """A process-local cancellation handle for one already-authorized turn."""

    session_id: uuid.UUID
    owner_user_id: uuid.UUID
    cancellation: PostgresExecutionCancellation
    registered_at: float = 0.0


_active_database_turns: dict[uuid.UUID, _ActiveDatabaseTurn] = {}
_active_database_turns_lock = asyncio.Lock()


async def _register_database_turn(
    *,
    turn_id: uuid.UUID,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    cancellation: PostgresExecutionCancellation,
) -> None:
    async with _active_database_turns_lock:
        now = time.monotonic()
        for other_id, other in list(_active_database_turns.items()):
            if other.session_id != session_id:
                continue
            if now - other.registered_at > STALE_TURN_SECONDS:
                _active_database_turns.pop(other_id, None)
                continue
            raise TurnInProgressError()
        _active_database_turns[turn_id] = _ActiveDatabaseTurn(
            session_id=session_id,
            owner_user_id=owner_user_id,
            cancellation=cancellation,
            registered_at=now,
        )


async def _unregister_database_turn(turn_id: uuid.UUID | None) -> None:
    if turn_id is None:
        return
    async with _active_database_turns_lock:
        _active_database_turns.pop(turn_id, None)


async def _active_database_turn_for_owner(
    *, turn_id: uuid.UUID, session_id: uuid.UUID, owner_user_id: uuid.UUID
) -> _ActiveDatabaseTurn | None:
    async with _active_database_turns_lock:
        turn = _active_database_turns.get(turn_id)
        if (
            turn is None
            or turn.session_id != session_id
            or turn.owner_user_id != owner_user_id
        ):
            return None
        return turn


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


# ── source-bound database session lifecycle ────────────────────────────────

@router.post(
    "/sources/{source_id}/chat/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a JWT-owned chat session against a PostgreSQL source",
)
@limiter.limit(get_settings().rate_limit_session_create)
async def create_database_session(
    request: Request,
    source_id: uuid.UUID,
    body: CreateSessionRequest,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SessionResponse:
    """Create a source binding without treating the source as a dataset.

    The explicit JWT dependency is intentional even though this router also
    supports legacy file-chat API keys.  A legacy credential has no source
    owner and therefore can never create a database transcript.
    """

    created = await manager.create_database_session_for_access(
        session_db=session,
        source_id=source_id,
        # Database chat has a single controlled planner/executor path; retain
        # AUTO on the session for compatibility, not a user-selectable RAG/
        # Polars override.
        mode=ConversationMode.AUTO,
        access=manager.ConversationAccess.for_user(current_user.id),
    )
    return SessionResponse(session=created)


@router.get(
    "/sources/{source_id}/chat/sessions",
    response_model=SessionListResponse,
    summary="List JWT-owned chat sessions for a PostgreSQL source",
)
async def list_database_sessions(
    source_id: uuid.UUID,
    session: DBSession,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
) -> SessionListResponse:
    sessions = await manager.list_sessions_for_source(
        session,
        source_id,
        limit=limit,
        access=manager.ConversationAccess.for_user(current_user.id),
    )
    return SessionListResponse(sessions=sessions, count=len(sessions))


@router.post(
    "/chat/sessions/{session_id}/turns/{turn_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    summary="Cancel an active controlled PostgreSQL chat turn",
)
async def cancel_database_turn(
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Cancel only a live, same-process source-bound turn owned by this JWT.

    This is intentionally not a retry/replay facility. The handle exists
    only while the original SSE request is alive and calls Stage 4B's
    connection-termination cancellation primitive. If a source is deleted
    mid-turn, the owner-bound live handle is still allowed to terminate that
    already-authorized connection; it can never retrieve the deleted session.
    """

    active_turn = await _active_database_turn_for_owner(
        turn_id=turn_id,
        session_id=session_id,
        owner_user_id=current_user.id,
    )
    if active_turn is None:
        raise NotFoundError("database chat turn not found")
    try:
        conversation = await manager.get_session_for_access(
            session,
            session_id,
            access=manager.ConversationAccess.for_user(current_user.id),
        )
    except NotFoundError:
        # Deleting a source/session cascades its transcript before this
        # request can reauthorize it. The live registry was created only for
        # this exact JWT owner and source-bound session, so it remains safe to
        # terminate the already-authorized remote connection without reading
        # any deleted resource or exposing a result.
        await active_turn.cancellation.cancel()
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if conversation.source is None:
        raise NotFoundError("database chat turn not found")
    await active_turn.cancellation.cancel()
    return Response(status_code=status.HTTP_202_ACCEPTED)


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
                "of start / intent / citations / query_result / token / done / error "
                "for file chat, or planning / executing / clarification / unsupported / "
                "catalog / postgres_result for source-bound database chat."
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

    is_database_chat = conversation.source is not None
    cancellation = PostgresExecutionCancellation() if is_database_chat else None
    # The browser receives this identifier in the ``start`` event.  It is
    # registered only for a live database SSE stream and is deliberately
    # opaque: the cancellation endpoint also verifies both session ownership
    # and the source-bound session before it touches the handle.
    turn_id = uuid.uuid4() if is_database_chat else None
    stream_finished = asyncio.Event()
    if is_database_chat and access.user_id is not None and turn_id is not None and cancellation is not None:
        # Claim the session before anything is written: a second tab gets 409 and leaves no message behind.
        await _register_database_turn(
            turn_id=turn_id, session_id=session_id, owner_user_id=access.user_id, cancellation=cancellation
        )
    claimed_turn = is_database_chat and access.user_id is not None

    async def watch_disconnect() -> None:
        """Terminate the dedicated client DB connection on a dropped SSE stream."""
        if cancellation is None:
            return
        try:
            while not stream_finished.is_set():
                if await request.is_disconnected():
                    await cancellation.cancel()
                    return
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A watcher failure is never an authorization bypass. The normal
            # generator cancellation path still terminates the connection.
            return

    async def event_stream() -> AsyncIterator[bytes]:
        watcher = asyncio.create_task(watch_disconnect()) if is_database_chat else None
        registered_turn = claimed_turn
        try:
            if is_database_chat:
                # A source session can only have a JWT-derived owner. The
                # manager rejects legacy access above; the assertion keeps the
                # typed executor boundary explicit rather than treating None
                # as a source owner.
                if access.user_id is None or cancellation is None:
                    return
                if turn_id is None:  # Defensive narrowing for the registry.
                    return
                stream = stream_postgres_chat_turn(
                    session_id=session_id,
                    user_message=body.message,
                    session_db=session,
                    settings=settings,
                    owner_user_id=access.user_id,
                    cancellation=cancellation,
                    authorized_session=conversation.session,
                    authorized_source=conversation.source,
                    turn_id=turn_id,
                )
            else:
                # Existing uploaded-file path remains unchanged.
                stream = stream_chat_turn(
                    session_id=session_id,
                    user_message=body.message,
                    session_db=session,
                    settings=settings,
                    authorized_session=conversation.session,
                    authorized_dataset=conversation.dataset,
                )
            if is_database_chat:
                async for event in _with_heartbeat(stream, settings.chat_sse_heartbeat_seconds):
                    if event is None:
                        yield SSE_KEEPALIVE
                        continue
                    if cancellation is not None and await request.is_disconnected():
                        await cancellation.cancel()
                        return
                    yield _encode_sse(event)
            else:
                async for event in stream:
                    yield _encode_sse(event)
        except asyncio.CancelledError:
            if cancellation is not None:
                await cancellation.cancel()
            raise
        except Exception as exc:  # pragma: no cover — top-level safety
            # If the agent itself crashes (it tries hard not to), emit
            # one terminal error frame so the client closes cleanly
            # rather than hanging on a dead connection.
            if is_database_chat:
                # Do not stringify/log a database-chat exception: a driver or
                # malformed result exception can contain customer data.
                logger.error("chat_routes.database_stream_crashed", session_id=str(session_id))
            else:
                logger.exception(
                    "chat_routes.stream_crashed",
                    session_id=str(session_id),
                    error=str(exc),
                )
            yield _encode_sse_raw(
                event="error",
                data=(
                    {"code": "internal_error", "message": nl_messages.INTERNAL}
                    if is_database_chat
                    else {"message": "internal error in chat stream"}
                ),
            )
            yield _encode_sse_raw(event="done", data={})
        finally:
            stream_finished.set()
            if registered_turn:
                await _unregister_database_turn(turn_id)
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # Safety net for a stream that is never iterated (the client vanished first): free the session's slot.
        background=BackgroundTask(_unregister_database_turn, turn_id) if claimed_turn else None,
        headers={
            # Tell reverse proxies (nginx) not to buffer the stream so
            # the client sees tokens as they are produced.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── SSE encoding ────────────────────────────────────────────────────────────

# An SSE comment: ignored by every parser, but it is bytes on the wire, so an idle-timeout proxy keeps the stream open.
SSE_KEEPALIVE = b": keepalive\n\n"


async def _with_heartbeat(
    stream: AsyncIterator[ChatStreamEvent], interval: float
) -> AsyncIterator[ChatStreamEvent | None]:
    """Yield the stream's events, and ``None`` whenever ``interval`` seconds pass without one (SPEC-10 W1.1).

    One ``__anext__()`` task stays pending across heartbeats. It is never cancelled on a timeout: that would cancel
    the model call or the database query running inside the turn. It is cancelled only when this wrapper itself is
    closed (disconnect or shutdown).
    """
    iterator = stream.__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            finished, pending = pending, None
            try:
                event = finished.result()
            except StopAsyncIteration:
                return
            yield event
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


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
