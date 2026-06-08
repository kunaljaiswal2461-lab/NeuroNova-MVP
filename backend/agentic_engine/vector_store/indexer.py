"""Indexer — Layer 6 write entrypoint.

Given a :class:`FindingsReport` and a DB session, embed every Finding
and persist the result to ``finding_embeddings``. Idempotent:
re-indexing a dataset replaces its rows wholesale within a single
transaction, so partial failures cannot leave stale data behind.

Failure handling mirrors Layer 5 — the indexer never raises on
missing-embedder or missing-session. Instead it returns an
:class:`IndexReport` with ``degraded=True`` so the calling pipeline can
still mark the dataset COMPLETE.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.finding_embedding import EMBEDDING_DIMENSION
from agentic_engine.findings.models import FindingsReport
from agentic_engine.llm_engine.base_llm import LLMUnavailable
from agentic_engine.vector_store import store
from agentic_engine.vector_store.chunking import chunk_findings
from agentic_engine.vector_store.embedder import Embedder, build_embedder
from agentic_engine.vector_store.models import IndexReport


logger = get_logger("vector_store.indexer")


# Hard cap on how many findings get embedded per dataset. Protects
# against pathological reports and bounds the per-dataset cost.
_MAX_FINDINGS_TO_INDEX: int = 1000


async def build_index(
    findings: FindingsReport,
    *,
    session: AsyncSession | None,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> IndexReport:
    """Embed + persist every Finding for a dataset.

    Args:
        findings:  the Layer 3 output to index.
        session:   active async DB session. If ``None``, the indexer
                   degrades gracefully and returns an empty report — used
                   in tests and the no-DB smoke harness.
        embedder:  optional embedder; if absent, one is built from
                   ``settings.openai_api_key`` and the indexer degrades
                   when no key is configured.
        settings:  override for the global Settings (test injection).
    """
    settings = settings or get_settings()
    dataset_id = findings.dataset_id

    # ── Degrade fast: no DB session means no persistent index. ─────────
    if session is None:
        logger.warning(
            "indexer.degraded.no_session",
            dataset_id=str(dataset_id),
        )
        return _degraded_report(
            dataset_id,
            reason="no database session provided",
            model_name=(embedder.model_name if embedder else "n/a"),
        )

    # ── Degrade fast: no embedder means no vectors. ────────────────────
    eff_embedder = embedder if embedder is not None else build_embedder(
        settings.openai_api_key
    )
    if eff_embedder is None:
        logger.warning(
            "indexer.degraded.no_api_key",
            dataset_id=str(dataset_id),
        )
        return _degraded_report(
            dataset_id,
            reason="OPENAI_API_KEY is not configured",
            model_name=settings.openai_embed_model,
        )

    # ── Sanity-check dimensionality before we start writing rows. ──────
    if eff_embedder.dimension != EMBEDDING_DIMENSION:
        # This indicates a configuration error (e.g. someone wired
        # text-embedding-3-large without bumping the SQL column). Fail
        # loud rather than silently writing rows the SQL column cannot
        # accept.
        raise RuntimeError(
            f"embedder dimension {eff_embedder.dimension} does not match "
            f"finding_embeddings column dimension {EMBEDDING_DIMENSION}"
        )

    # ── Chunk + cap. ───────────────────────────────────────────────────
    all_chunks = chunk_findings(findings)
    total_findings = len(findings.findings)
    skipped_count = total_findings - len(all_chunks)

    if len(all_chunks) > _MAX_FINDINGS_TO_INDEX:
        # Keep the order-of-arrival head; HIGH/MEDIUM tend to come first
        # because the findings builder runs nullability/constant/etc. ahead
        # of low-priority recommendations.
        skipped_count += len(all_chunks) - _MAX_FINDINGS_TO_INDEX
        all_chunks = all_chunks[:_MAX_FINDINGS_TO_INDEX]

    if not all_chunks:
        logger.info("indexer.no_chunks", dataset_id=str(dataset_id))
        # Still clear the table — an upstream re-profile may have removed
        # findings that we previously indexed.
        await store.delete_dataset_embeddings(session, dataset_id)
        await session.commit()
        return IndexReport(
            dataset_id=dataset_id,
            indexed_count=0,
            skipped_count=skipped_count,
            model_name=eff_embedder.model_name,
            embedding_dimension=eff_embedder.dimension,
        )

    # ── Embed. The embedder raises LLMUnavailable on permanent failure. ─
    try:
        vectors = await eff_embedder.embed([c.text for c in all_chunks])
    except LLMUnavailable as exc:
        logger.warning(
            "indexer.embed_failed",
            dataset_id=str(dataset_id),
            error=str(exc),
        )
        return _degraded_report(
            dataset_id,
            reason=f"embedder unavailable: {exc}",
            model_name=eff_embedder.model_name,
        )

    # ── Atomic replace. ─────────────────────────────────────────────────
    deleted = await store.delete_dataset_embeddings(session, dataset_id)
    inserted = await store.insert_embeddings(
        session,
        dataset_id=dataset_id,
        chunks=all_chunks,
        vectors=vectors,
        model_name=eff_embedder.model_name,
    )
    await session.commit()

    logger.info(
        "indexer.built",
        dataset_id=str(dataset_id),
        inserted=inserted,
        deleted=deleted,
        skipped=skipped_count,
        model=eff_embedder.model_name,
    )

    return IndexReport(
        dataset_id=dataset_id,
        indexed_count=inserted,
        skipped_count=skipped_count,
        model_name=eff_embedder.model_name,
        embedding_dimension=eff_embedder.dimension,
    )


# ── helpers ─────────────────────────────────────────────────────────────────

def _degraded_report(
    dataset_id: uuid.UUID,
    *,
    reason: str,
    model_name: str,
) -> IndexReport:
    return IndexReport(
        dataset_id=dataset_id,
        indexed_count=0,
        skipped_count=0,
        model_name=model_name,
        embedding_dimension=EMBEDDING_DIMENSION,
        degraded=True,
        degraded_reason=reason,
    )
