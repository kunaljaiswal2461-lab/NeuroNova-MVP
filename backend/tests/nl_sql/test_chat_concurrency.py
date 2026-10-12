"""SPEC-09 L1-7 / I9: one database turn per session; a second POST gets 409 and writes nothing."""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.api.routes import chat_routes
from agentic_engine.conversational.models import ChatStreamEvent, ChatStreamEventType
from tests.conversational.test_postgres_chat_routes import _headers, source_chat_context  # noqa: F401


@pytest.mark.asyncio
async def test_a_second_turn_on_one_session_is_refused_before_anything_is_written(source_chat_context, monkeypatch):
    app, settings, owner, _other, fakes = source_chat_context
    started, release = asyncio.Event(), asyncio.Event()
    writes: list[str] = []

    async def slow_stream(**kwargs):
        writes.append(kwargs["user_message"])               # stands in for the committed user message
        started.set()
        yield ChatStreamEvent(event=ChatStreamEventType.PLANNING, data={"state": "planning"})
        await release.wait()
        yield ChatStreamEvent(event=ChatStreamEventType.TOKEN, data={"text": "first"})
        yield ChatStreamEvent(event=ChatStreamEventType.DONE, data={})

    monkeypatch.setattr(chat_routes, "stream_postgres_chat_turn", slow_stream)
    url = f"/api/v1/chat/sessions/{fakes.session_id}/message"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = _headers(settings, owner)
        first = asyncio.create_task(client.post(url, headers=headers, json={"message": "first question"}))
        await asyncio.wait_for(started.wait(), 5)

        second = await client.post(url, headers=headers, json={"message": "second question"})
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "turn_in_progress"
        assert writes == ["first question"]                  # the refused turn never reached the agent

        release.set()
        done = await asyncio.wait_for(first, 5)
        assert done.status_code == 200 and "first" in done.text

        third = await client.post(url, headers=headers, json={"message": "third question"})
        assert third.status_code == 200                      # the slot is free again
        assert writes == ["first question", "third question"]


@pytest.mark.asyncio
async def test_different_sessions_do_not_block_each_other():
    cancel_a, cancel_b = chat_routes.PostgresExecutionCancellation(), chat_routes.PostgresExecutionCancellation()
    owner = uuid.uuid4()
    one, two = uuid.uuid4(), uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    await chat_routes._register_database_turn(turn_id=a, session_id=one, owner_user_id=owner, cancellation=cancel_a)
    try:
        await chat_routes._register_database_turn(turn_id=b, session_id=two, owner_user_id=owner, cancellation=cancel_b)
        with pytest.raises(chat_routes.TurnInProgressError):
            await chat_routes._register_database_turn(
                turn_id=uuid.uuid4(), session_id=one, owner_user_id=owner, cancellation=cancel_a
            )
    finally:
        await chat_routes._unregister_database_turn(a)
        await chat_routes._unregister_database_turn(b)


@pytest.mark.asyncio
async def test_an_abandoned_registration_goes_stale(monkeypatch):
    owner, session = uuid.uuid4(), uuid.uuid4()
    first = uuid.uuid4()
    cancel = chat_routes.PostgresExecutionCancellation()
    await chat_routes._register_database_turn(turn_id=first, session_id=session, owner_user_id=owner, cancellation=cancel)
    monkeypatch.setattr(chat_routes, "STALE_TURN_SECONDS", -1)
    second = uuid.uuid4()
    try:
        await chat_routes._register_database_turn(turn_id=second, session_id=session, owner_user_id=owner, cancellation=cancel)
    finally:
        await chat_routes._unregister_database_turn(first)
        await chat_routes._unregister_database_turn(second)
