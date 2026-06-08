"""Shared fixtures for Layer 7 unit tests.

The Layer 6 retriever needs a real Postgres+pgvector to exercise the
cosine query (that lives in an integration test, deferred). For unit
tests we patch the retriever module with an AsyncMock so the chat
agent can be tested in full isolation.

The chat path also needs a streaming LLM stub — we extend Layer 5's
FakeLLM idea with a ``chat_stream`` method that yields a scripted
sequence of :class:`StreamDelta`s.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import polars as pl
import pytest

from app.core.config import Settings
from app.db.models.dataset import FileType
from agentic_engine.conversational.models import (
    ChatMessage,
    ChatRole,
    ChatStreamEvent,
    ConversationMode,
    ConversationSession,
)
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.llm_engine.base_llm import (
    ChatResult,
    LLMUnavailable,
    StreamDelta,
)
from agentic_engine.vector_store.models import (
    RetrievalHit,
    RetrievalResult,
)


# ── FakeStreamingLLM ────────────────────────────────────────────────────────

@dataclass
class _RecordedJsonCall:
    model: str
    system: str
    user: str
    max_tokens: int
    temperature: float


@dataclass
class _RecordedStreamCall:
    model: str
    system: str
    user: str
    history: list[dict[str, str]] | None
    max_tokens: int
    temperature: float


@dataclass
class FakeStreamingLLM:
    """LLMClient stub for both JSON and streaming modes.

    Use ``json_responses`` to script ``chat_json`` returns (dicts or
    Exceptions) and ``stream_chunks`` to script ``chat_stream`` output
    (each entry is an iterable of strings → text deltas, or an
    Exception → raised at the start of the stream).
    """

    json_responses: list[Any] = field(default_factory=list)
    stream_chunks: list[Any] = field(default_factory=list)

    json_calls: list[_RecordedJsonCall] = field(default_factory=list)
    stream_calls: list[_RecordedStreamCall] = field(default_factory=list)

    input_tokens_per_call: int = 100
    output_tokens_per_call: int = 200

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> ChatResult:
        self.json_calls.append(
            _RecordedJsonCall(
                model=model, system=system, user=user,
                max_tokens=max_tokens, temperature=temperature,
            )
        )
        if not self.json_responses:
            raise AssertionError("FakeStreamingLLM ran out of json_responses")
        nxt = self.json_responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return ChatResult(
            content_json=nxt,
            model=model,
            input_tokens=self.input_tokens_per_call,
            output_tokens=self.output_tokens_per_call,
        )

    async def chat_stream(
        self,
        *,
        model: str,
        system: str,
        user: str,
        history: list[dict[str, str]] | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.4,
    ) -> AsyncIterator[StreamDelta]:
        self.stream_calls.append(
            _RecordedStreamCall(
                model=model, system=system, user=user, history=history,
                max_tokens=max_tokens, temperature=temperature,
            )
        )
        if not self.stream_chunks:
            raise AssertionError("FakeStreamingLLM ran out of stream_chunks")
        nxt = self.stream_chunks.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        for chunk in nxt:
            yield StreamDelta(text=chunk, finished=False)
        yield StreamDelta(
            text="",
            finished=True,
            input_tokens=self.input_tokens_per_call,
            output_tokens=self.output_tokens_per_call,
            model=model,
        )


# ── FakeRetriever — used by RAG tests via monkeypatch ───────────────────────

@dataclass
class FakeRetrieverConfig:
    """Knobs the patched retriever observes — set per test."""

    hits: list[RetrievalHit] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None


def install_fake_retriever(
    monkeypatch: pytest.MonkeyPatch,
    config: FakeRetrieverConfig,
) -> list[dict[str, Any]]:
    """Patch the retrieve function on every module that imported it.

    Returns a list captured by the patched function so tests can assert
    on the queries it received.
    """
    captured: list[dict[str, Any]] = []

    async def _fake_retrieve(dataset_id, query, **kwargs):
        captured.append({
            "dataset_id": dataset_id,
            "query": query.query,
            "top_k": query.top_k,
            "min_similarity": query.min_similarity,
            "severity": query.severity,
            "finding_type": query.finding_type,
            "column": query.column,
        })
        return RetrievalResult(
            dataset_id=dataset_id,
            query=query.query,
            hits=config.hits,
            model_used="fake-embed-3-small",
            degraded=config.degraded,
            degraded_reason=config.degraded_reason,
        )

    # Patch the retriever module itself, and every module that imported
    # ``retrieve`` directly so import-time bindings see the fake too.
    from agentic_engine.vector_store import retriever as retriever_module
    from agentic_engine.conversational import rag_answerer as rag_module

    monkeypatch.setattr(retriever_module, "retrieve", _fake_retrieve)
    monkeypatch.setattr(rag_module, "run_retrieve", _fake_retrieve)
    return captured


# ── canned retrieval hits ───────────────────────────────────────────────────

def make_hit(
    *,
    similarity: float = 0.85,
    severity: Severity = Severity.HIGH,
    finding_type: FindingType = FindingType.HIGH_NULLABILITY,
    column: str | None = "revenue",
    text: str = "High null rate in revenue (60%)",
) -> RetrievalHit:
    return RetrievalHit(
        finding_id=uuid.uuid4(),
        similarity=similarity,
        severity=severity,
        finding_type=finding_type,
        column=column,
        confidence=0.9,
        embedded_text=text,
    )


# ── settings ────────────────────────────────────────────────────────────────

@pytest.fixture
def settings_with_key(tmp_path) -> Settings:
    return Settings(
        api_key="test-api-key-12345",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        openai_api_key="sk-test-fake-key",
        data_dir=tmp_path,
    )


@pytest.fixture
def settings_no_key(tmp_path) -> Settings:
    return Settings(
        api_key="test-api-key-12345",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        openai_api_key="",
        data_dir=tmp_path,
    )


# ── tiny CSV on disk for query-executor tests ───────────────────────────────

@pytest.fixture
def tiny_csv(tmp_path: Path) -> Path:
    """Write a 5-row 4-column CSV the query executor can read."""
    target = tmp_path / "tiny.csv"
    df = pl.DataFrame({
        "country": ["US", "US", "UK", "DE", "US"],
        "revenue": [100.0, 250.0, 75.0, 180.0, 9000.0],
        "price": [10.0, 25.0, 8.0, 18.0, 90.0],
        "category": ["A", "B", "A", "B", "C"],
    })
    df.write_csv(target)
    return target


# ── schema snapshot fixture ─────────────────────────────────────────────────

@pytest.fixture
def tiny_schema() -> list[dict[str, Any]]:
    return [
        {"name": "country", "dtype": "Utf8", "semantic_type": "GEOGRAPHIC"},
        {"name": "revenue", "dtype": "Float64", "semantic_type": "FINANCIAL"},
        {"name": "price", "dtype": "Float64", "semantic_type": "FINANCIAL"},
        {"name": "category", "dtype": "Utf8", "semantic_type": "CATEGORICAL"},
    ]


# ── canned response builders for codegen + intent ──────────────────────────

def codegen_response(expression: str, explanation: str = "(test)") -> dict[str, Any]:
    return {"expression": expression, "explanation": explanation}


def intent_response(mode: str, confidence: float = 0.8, rationale: str = "(test)") -> dict[str, Any]:
    return {"mode": mode, "confidence": confidence, "rationale": rationale}


__all__ = [
    "FakeRetrieverConfig",
    "FakeStreamingLLM",
    "LLMUnavailable",
    "codegen_response",
    "install_fake_retriever",
    "intent_response",
    "make_hit",
]
