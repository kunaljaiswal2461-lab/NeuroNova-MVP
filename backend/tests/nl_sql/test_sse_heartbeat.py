"""SPEC-10 W1.1 / I4 / E42: the keep-alive wrapper never cancels the turn, and the crash frame is a fixed message."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.api.routes import chat_routes
from app.services.nl_sql import messages
from agentic_engine.conversational.models import ChatStreamEvent, ChatStreamEventType
from tests.conversational.test_postgres_chat_routes import _headers, source_chat_context  # noqa: F401


def event(kind: ChatStreamEventType, **data) -> ChatStreamEvent:
    return ChatStreamEvent(event=kind, data=data)


class SlowTurn:
    """A stream whose middle step is a long 'model call'; records whether that call ran to completion."""

    def __init__(self, delay: float):
        self.delay, self.started, self.completed, self.cancelled = delay, 0, 0, 0

    async def __call__(self, **_kwargs):
        yield event(ChatStreamEventType.EXECUTING, state="executing")
        self.started += 1
        try:
            await asyncio.sleep(self.delay)
            self.completed += 1
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        yield event(ChatStreamEventType.PRESENTING, state="presenting")
        yield event(ChatStreamEventType.DONE)


async def _broken_heartbeat(stream, interval):
    """The mistake the spec warns about: cancelling the pending ``__anext__`` on every timeout."""
    iterator = stream.__aiter__()
    while True:
        try:
            yield await asyncio.wait_for(iterator.__anext__(), timeout=interval)
        except asyncio.TimeoutError:
            yield None
        except StopAsyncIteration:
            return


@pytest.mark.parametrize(
    "impl",
    [
        chat_routes._with_heartbeat,
        pytest.param(
            _broken_heartbeat,
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason="negative control for I4: cancels the model call"),
        ),
    ],
)
async def test_a_heartbeat_never_cancels_the_in_flight_call(impl):
    turn = SlowTurn(delay=0.3)
    seen = [item async for item in impl(turn(), 0.05)]
    beats = [item for item in seen if item is None]
    assert len(beats) >= 3, "heartbeats must be written while the call runs"
    assert (turn.started, turn.completed, turn.cancelled) == (1, 1, 0)
    assert [e.event.value for e in seen if e is not None] == ["executing", "presenting", "done"]


async def test_closing_the_wrapper_cancels_the_pending_call_and_closes_the_stream():
    turn = SlowTurn(delay=30)
    wrapper = chat_routes._with_heartbeat(turn(), 0.05)
    assert (await wrapper.__anext__()).event.value == "executing"
    assert await wrapper.__anext__() is None
    await wrapper.aclose()
    assert turn.cancelled == 1 and turn.completed == 0


@pytest.mark.asyncio
async def test_the_route_writes_keepalive_comments_between_events(source_chat_context, monkeypatch):
    app, settings, owner, _other, fakes = source_chat_context
    monkeypatch.setattr(settings, "chat_sse_heartbeat_seconds", 0.05)
    turn = SlowTurn(delay=0.3)
    monkeypatch.setattr(chat_routes, "stream_postgres_chat_turn", turn)
    url = f"/api/v1/chat/sessions/{fakes.session_id}/message"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(url, headers=_headers(settings, owner), json={"message": "q"})
    body = response.text
    assert response.status_code == 200 and body.count(": keepalive\n\n") >= 3
    assert body.index("event: executing") < body.index(": keepalive") < body.index("event: presenting")
    assert (turn.completed, turn.cancelled) == (1, 0)


@pytest.mark.asyncio
async def test_a_route_crash_is_a_fixed_internal_error_frame(source_chat_context, monkeypatch):
    """E42: no exception text; a code; then `done`."""
    app, settings, owner, _other, fakes = source_chat_context

    async def crashing(**_kwargs):
        yield event(ChatStreamEventType.PLANNING, state="planning")
        raise RuntimeError("secret driver detail: password=hunter2")

    monkeypatch.setattr(chat_routes, "stream_postgres_chat_turn", crashing)
    url = f"/api/v1/chat/sessions/{fakes.session_id}/message"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(url, headers=_headers(settings, owner), json={"message": "q"})
    body = response.text
    assert "hunter2" not in body and "driver" not in body
    assert f'event: error\ndata: {{"code": "internal_error", "message": "{messages.INTERNAL}"}}\n\n' in body
    assert body.rstrip().endswith("event: done\ndata: {}")
