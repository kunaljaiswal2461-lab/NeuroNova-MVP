"""Shared fixtures for Layer 6 unit tests.

The store layer needs a real PostgreSQL+pgvector to exercise the actual
cosine query; that lives in an integration test (deferred). Here we
patch ``vector_store.store`` with async-mock equivalents so the indexer
and retriever orchestrators can be tested in isolation.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding, FindingsReport
from agentic_engine.llm_engine.base_llm import LLMUnavailable
from agentic_engine.vector_store.store import StoredHit


# ── FakeEmbedder ─────────────────────────────────────────────────────────────

@dataclass
class _RecordedEmbed:
    texts: list[str]


@dataclass
class FakeEmbedder:
    """Deterministic embedder for tests.

    Each text is hashed into a fixed-dimension vector. Identical inputs
    yield identical outputs; small input changes produce different
    vectors. Optionally raises :class:`LLMUnavailable` to test the
    degraded path.
    """

    dimension: int = 1536
    model_name: str = "fake-embed-3-small"
    raise_on_embed: BaseException | None = None
    calls: list[_RecordedEmbed] = field(default_factory=list)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(_RecordedEmbed(texts=list(texts)))
        if self.raise_on_embed is not None:
            raise self.raise_on_embed
        return [_deterministic_vector(t, self.dimension) for t in texts]


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """Hash-derived pseudo-random vector in [-1, 1]^dim."""
    seed = int.from_bytes(
        hashlib.sha256(text.encode("utf-8")).digest()[:8], "big"
    )
    out: list[float] = []
    # A cheap LCG produces enough spread for similarity tests without
    # bringing numpy into the test surface.
    value = seed
    for _ in range(dim):
        value = (value * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        # Map to [-1, 1].
        out.append((value / (1 << 64)) * 2.0 - 1.0)
    return out


# ── FindingsReport fixtures ──────────────────────────────────────────────────

@pytest.fixture
def dataset_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def small_findings_report(dataset_id) -> FindingsReport:
    """Three findings — one HIGH, one MEDIUM, one LOW — across two columns."""
    return FindingsReport(
        dataset_id=dataset_id,
        generated_at=datetime.now(timezone.utc),
        findings=[
            Finding(
                finding_id=uuid.uuid4(),
                type=FindingType.HIGH_NULLABILITY, severity=Severity.HIGH,
                confidence=0.92, column="revenue",
                title="High null rate in 'revenue' (60%)",
                description="Column 'revenue' has 60% missing values.",
                evidence={"null_pct": 60.0},
                semantic_context="FINANCIAL",
            ),
            Finding(
                finding_id=uuid.uuid4(),
                type=FindingType.STRONG_CORRELATION, severity=Severity.MEDIUM,
                confidence=0.88, column=None,
                title="Strong correlation price <-> revenue",
                description="pearson=0.95",
                evidence={"col_a": "price", "col_b": "revenue"},
            ),
            Finding(
                finding_id=uuid.uuid4(),
                type=FindingType.SEMANTIC_TAG, severity=Severity.LOW,
                confidence=0.85, column="country",
                title="country: GEOGRAPHIC",
                description="",
                evidence={"semantic_type": "GEOGRAPHIC"},
                semantic_context="GEOGRAPHIC",
            ),
        ],
    )


@pytest.fixture
def empty_findings_report(dataset_id) -> FindingsReport:
    return FindingsReport(
        dataset_id=dataset_id,
        generated_at=datetime.now(timezone.utc),
        findings=[],
    )


# ── settings ─────────────────────────────────────────────────────────────────

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


# ── store mock helpers ───────────────────────────────────────────────────────

@pytest.fixture
def fake_session() -> AsyncMock:
    """An AsyncMock standing in for ``AsyncSession``.

    ``session.commit()`` returns None; we only care that it is called.
    """
    session = AsyncMock()
    return session


@pytest.fixture
def patch_store(monkeypatch) -> dict[str, AsyncMock]:
    """Patch every async function in ``vector_store.store`` with an AsyncMock.

    Yields a dict so tests can assert on call counts / arguments.
    """
    from agentic_engine.vector_store import store

    mocks = {
        "delete_dataset_embeddings": AsyncMock(return_value=0),
        "insert_embeddings": AsyncMock(),
        "cosine_search": AsyncMock(return_value=[]),
        "count_dataset_embeddings": AsyncMock(return_value=0),
    }

    # ``insert_embeddings`` should return the count of rows it inserted
    # so the indexer's logging stays meaningful — derive it from the
    # ``chunks`` kwarg.
    async def _insert(session, *, dataset_id, chunks, vectors, model_name):
        return len(chunks)
    mocks["insert_embeddings"].side_effect = _insert

    for name, mock in mocks.items():
        monkeypatch.setattr(store, name, mock)
    return mocks


# ── canned StoredHit builders ────────────────────────────────────────────────

def make_stored_hit(
    *,
    finding_id: uuid.UUID | None = None,
    similarity: float = 0.85,
    severity: Severity = Severity.HIGH,
    finding_type: FindingType = FindingType.HIGH_NULLABILITY,
    column: str | None = "revenue",
    confidence: float = 0.9,
    text: str = "High null rate in revenue (60%)",
) -> StoredHit:
    return StoredHit(
        finding_id=finding_id or uuid.uuid4(),
        similarity=similarity,
        severity=severity.value,
        finding_type=finding_type.value,
        column_name=column,
        confidence=confidence,
        embedded_text=text,
    )


__all__ = [
    "FakeEmbedder",
    "LLMUnavailable",
    "make_stored_hit",
]
