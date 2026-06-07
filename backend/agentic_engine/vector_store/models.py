"""Pydantic models for the Layer 6 retrieval API.

These are the shapes used by the indexer, retriever, route handlers, and
unit tests. They are deliberately small: the heavy lifting (Finding
content) lives on disk and is hydrated by the caller only when needed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentic_engine.findings.finding_types import FindingType, Severity


# ── indexing-side models ─────────────────────────────────────────────────────

class FindingChunk(BaseModel):
    """One pre-embedding unit: the text plus the metadata that will be
    stored alongside the vector.

    Created by :mod:`agentic_engine.vector_store.chunking` and consumed by
    :mod:`agentic_engine.vector_store.indexer`.
    """

    finding_id: uuid.UUID
    text: str
    severity: Severity
    finding_type: FindingType
    column: str | None = None
    confidence: float

    model_config = ConfigDict(use_enum_values=False)


class IndexReport(BaseModel):
    """Summary returned by the indexer after a successful build."""

    dataset_id: uuid.UUID
    indexed_count: int
    skipped_count: int
    model_name: str
    embedding_dimension: int
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Set to True when the indexer ran but produced no embeddings — either
    # because there were no findings, the embedder was unavailable, or the
    # DB session was not provided. The pipeline treats this as a non-fatal
    # outcome and still marks the dataset COMPLETE.
    degraded: bool = False
    degraded_reason: str | None = None


# ── retrieval-side models ────────────────────────────────────────────────────

class RetrievalQuery(BaseModel):
    """Request shape for ``POST /datasets/{id}/retrieve``."""

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)

    # Drop results below this cosine similarity. 0.0 (default) means no
    # post-filter. Cosine similarity for OpenAI embeddings of unrelated
    # text typically sits around 0.05–0.20, so a value of 0.25–0.35 is a
    # reasonable "is this actually relevant" threshold.
    min_similarity: float = Field(default=0.0, ge=0.0, le=1.0)

    severity: Severity | None = None
    finding_type: FindingType | None = None
    column: str | None = Field(default=None, max_length=256)


class RetrievalHit(BaseModel):
    """One result of a similarity search."""

    finding_id: uuid.UUID
    similarity: float = Field(ge=0.0, le=1.0)

    severity: Severity
    finding_type: FindingType
    column: str | None = None
    confidence: float

    # Echo of the text that was embedded — useful for debugging and for
    # LLM citation rendering ("we found this because…").
    embedded_text: str


class RetrievalResult(BaseModel):
    """Top-level response for a single retrieval call."""

    dataset_id: uuid.UUID
    query: str
    hits: list[RetrievalHit] = Field(default_factory=list)
    model_used: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # If retrieval could not run (no embedder, no index for this dataset),
    # the result is empty and ``degraded`` is True. Callers — including
    # Layer 7 — should fall back to a non-RAG path in that case.
    degraded: bool = False
    degraded_reason: str | None = None

    @property
    def count(self) -> int:
        return len(self.hits)


# Lightweight convenience for tests / programmatic callers that want to
# construct a query inline.
def make_query(query: str, **overrides: Any) -> RetrievalQuery:
    return RetrievalQuery(query=query, **overrides)
