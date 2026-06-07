"""SQLAlchemy CRUD for ``finding_embeddings``.

Thin, intentional surface: three async functions used by the indexer and
retriever. No business logic — the orchestrators in ``indexer.py`` and
``retriever.py`` own that. Anyone else who needs to touch the
embeddings table goes through this module.

All operations are parameterised; nothing here ever interpolates user
input into SQL.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.finding_embedding import FindingEmbedding
from agentic_engine.vector_store.models import FindingChunk


# ── DTO returned by cosine_search ────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StoredHit:
    """Minimal row shape returned to the retriever.

    We deliberately do not return the full :class:`FindingEmbedding`
    ORM object so the retriever can stay session-agnostic and the
    pgvector column does not leak out of this module.
    """

    finding_id: uuid.UUID
    similarity: float
    severity: str
    finding_type: str
    column_name: str | None
    confidence: float
    embedded_text: str


# ── writes ───────────────────────────────────────────────────────────────────

async def delete_dataset_embeddings(
    session: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Remove every embedding row for a dataset. Returns rows deleted."""
    stmt = delete(FindingEmbedding).where(
        FindingEmbedding.dataset_id == dataset_id
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def insert_embeddings(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    chunks: list[FindingChunk],
    vectors: list[list[float]],
    model_name: str,
) -> int:
    """Bulk-insert one row per (chunk, vector) pair.

    The caller is expected to have already cleared any pre-existing rows
    for ``dataset_id`` (see :func:`delete_dataset_embeddings`) — the
    indexer wraps both operations in a single transaction so re-indexing
    is atomic.

    Returns the number of rows inserted.
    """
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks ({len(chunks)}) and vectors ({len(vectors)}) length mismatch"
        )
    if not chunks:
        return 0

    objects = [
        FindingEmbedding(
            dataset_id=dataset_id,
            finding_id=chunk.finding_id,
            embedding=vector,
            model_name=model_name,
            embedded_text=chunk.text,
            severity=chunk.severity.value,
            finding_type=chunk.finding_type.value,
            column_name=chunk.column,
            confidence=chunk.confidence,
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    session.add_all(objects)
    await session.flush()
    return len(objects)


# ── reads ────────────────────────────────────────────────────────────────────

async def count_dataset_embeddings(
    session: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """How many embedding rows exist for a dataset (cheap existence check)."""
    stmt = select(FindingEmbedding.id).where(
        FindingEmbedding.dataset_id == dataset_id
    )
    result = await session.execute(stmt)
    return len(result.all())


async def cosine_search(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    query_vector: list[float],
    top_k: int,
    severity: str | None = None,
    finding_type: str | None = None,
    column: str | None = None,
) -> list[StoredHit]:
    """Top-K nearest embeddings under cosine distance.

    ``embedding <=> query`` returns cosine distance in [0, 2], where 0 is
    identical. We project it as ``similarity = 1 - distance`` which sits
    in [-1, 1] — clamped to [0, 1] in :class:`StoredHit` because all our
    embeddings have non-negative cosine similarity in practice (text
    embeddings live in roughly the same hemisphere).
    """
    distance = FindingEmbedding.embedding.cosine_distance(query_vector)

    stmt = (
        select(
            FindingEmbedding.finding_id,
            FindingEmbedding.severity,
            FindingEmbedding.finding_type,
            FindingEmbedding.column_name,
            FindingEmbedding.confidence,
            FindingEmbedding.embedded_text,
            distance.label("distance"),
        )
        .where(FindingEmbedding.dataset_id == dataset_id)
    )

    # Pre-filters use indexed columns, so they narrow the candidate set
    # before the cosine sort runs. Always apply them when given.
    if severity is not None:
        stmt = stmt.where(FindingEmbedding.severity == severity)
    if finding_type is not None:
        stmt = stmt.where(FindingEmbedding.finding_type == finding_type)
    if column is not None:
        stmt = stmt.where(FindingEmbedding.column_name == column)

    stmt = stmt.order_by("distance").limit(top_k)

    result = await session.execute(stmt)
    rows = result.all()

    hits: list[StoredHit] = []
    for row in rows:
        similarity = max(0.0, min(1.0, 1.0 - float(row.distance)))
        hits.append(
            StoredHit(
                finding_id=row.finding_id,
                similarity=similarity,
                severity=row.severity,
                finding_type=row.finding_type,
                column_name=row.column_name,
                confidence=row.confidence,
                embedded_text=row.embedded_text,
            )
        )
    return hits
