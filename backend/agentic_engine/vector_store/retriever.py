"""Retriever — Layer 6 read entrypoint.

Embed the user query, run a cosine-similarity search against the stored
finding embeddings, and return a typed :class:`RetrievalResult`. Used by:

  * the ``POST /datasets/{id}/retrieve`` API route
  * Layer 7's RAG mode (chat agent grounding context)

The retriever degrades the same way the indexer does: missing embedder,
missing session, or an empty index all yield a structurally valid result
with ``degraded=True`` so Layer 7 can fall back cleanly.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from agentic_engine.llm_engine.base_llm import LLMUnavailable
from agentic_engine.vector_store import store
from agentic_engine.vector_store.embedder import Embedder, build_embedder
from agentic_engine.vector_store.models import (
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
)
from agentic_engine.findings.finding_types import FindingType, Severity


logger = get_logger("vector_store.retriever")


async def retrieve(
    dataset_id: uuid.UUID,
    query: RetrievalQuery,
    *,
    session: AsyncSession | None,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> RetrievalResult:
    """Run a similarity search against a dataset's finding embeddings."""
    settings = settings or get_settings()

    if session is None:
        return _empty(dataset_id, query, "no database session",
                      embedder.model_name if embedder else "n/a")

    eff_embedder = embedder if embedder is not None else build_embedder(
        settings.openai_api_key
    )
    if eff_embedder is None:
        return _empty(
            dataset_id, query,
            "OPENAI_API_KEY is not configured",
            settings.openai_embed_model,
        )

    # ── Embed the query. ───────────────────────────────────────────────
    try:
        embedded = await eff_embedder.embed([query.query])
    except LLMUnavailable as exc:
        logger.warning(
            "retriever.embed_failed",
            dataset_id=str(dataset_id),
            error=str(exc),
        )
        return _empty(
            dataset_id, query,
            f"embedder unavailable: {exc}",
            eff_embedder.model_name,
        )

    query_vector = embedded[0]

    # ── Cosine search with pre-filters. ────────────────────────────────
    raw_hits = await store.cosine_search(
        session,
        dataset_id=dataset_id,
        query_vector=query_vector,
        top_k=query.top_k,
        severity=query.severity.value if query.severity else None,
        finding_type=query.finding_type.value if query.finding_type else None,
        column=query.column,
    )

    # ── Post-filter by min_similarity, then format. ─────────────────────
    hits: list[RetrievalHit] = []
    for raw in raw_hits:
        if raw.similarity < query.min_similarity:
            continue
        hits.append(
            RetrievalHit(
                finding_id=raw.finding_id,
                similarity=raw.similarity,
                severity=Severity(raw.severity),
                finding_type=FindingType(raw.finding_type),
                column=raw.column_name,
                confidence=raw.confidence,
                embedded_text=raw.embedded_text,
            )
        )

    logger.info(
        "retriever.search",
        dataset_id=str(dataset_id),
        query_len=len(query.query),
        top_k=query.top_k,
        returned=len(hits),
        model=eff_embedder.model_name,
    )

    return RetrievalResult(
        dataset_id=dataset_id,
        query=query.query,
        hits=hits,
        model_used=eff_embedder.model_name,
    )


# ── helpers ─────────────────────────────────────────────────────────────────

def _empty(
    dataset_id: uuid.UUID,
    query: RetrievalQuery,
    reason: str,
    model: str,
) -> RetrievalResult:
    logger.warning(
        "retriever.degraded",
        dataset_id=str(dataset_id),
        reason=reason,
    )
    return RetrievalResult(
        dataset_id=dataset_id,
        query=query.query,
        hits=[],
        model_used=model,
        degraded=True,
        degraded_reason=reason,
    )
