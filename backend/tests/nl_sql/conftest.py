"""SPEC-08 L1 fixtures: a real published SQLite catalog (SPEC-07's `world`), a stub LLM that records every call."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agentic_engine.llm_engine.base_llm import ChatResult, LLMUnavailable
from app.services.nl_sql.prompt import CANARY
from tests.sample_values.conftest import FakeReader, factory, settings, world  # noqa: F401  (fixtures)

REF_TIME = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
ZONE = "Asia/Kolkata"

GOOD_SQL = {
    "kind": "sql",
    "sql": "SELECT count(*) AS n FROM sales.orders",
    "assumptions": [],
    "tables_used": ["sales.orders"],
    "output_intent": {
        "type": "kpi",
        "encodings": {"x": None, "y": [], "series": None, "label": None, "value": "n"},
    },
    "clarification": None,
    "reason_code": None,
    "reason": None,
}


def envelope(**changes: Any) -> dict:
    return {**GOOD_SQL, **changes}


@dataclass
class StubLLM:
    """Implements ``chat_structured`` only. Each response is a dict, a ChatResult or an exception."""

    responses: list[Any] = field(default_factory=lambda: [GOOD_SQL])
    calls: list[dict] = field(default_factory=list)

    async def chat_structured(self, **kwargs) -> ChatResult:
        self.calls.append(kwargs)
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ChatResult):
            return item
        return ChatResult(content_json=item, model="gpt-4o-test", input_tokens=100, output_tokens=20)

    async def chat_json(self, **kwargs):          # pass 1 must never use it
        raise AssertionError("pass 1 uses chat_structured")


@pytest.fixture
def llm() -> StubLLM:
    return StubLLM()


@pytest.fixture
async def synced(world):
    await world.sync(FakeReader())
    return world


async def run_pass1(w, llm, question: str, **kwargs):
    from app.services.nl_sql import generate_sql

    return await generate_sql(
        session_factory=w.factory, settings=w.settings, llm=llm, user_id=w.owner_id, source_id=w.source_id,
        question=question, reference_time=kwargs.pop("reference_time", REF_TIME),
        time_zone=kwargs.pop("time_zone", ZONE), **kwargs,
    )


__all__ = ["CANARY", "GOOD_SQL", "LLMUnavailable", "REF_TIME", "StubLLM", "ZONE", "envelope", "run_pass1"]
