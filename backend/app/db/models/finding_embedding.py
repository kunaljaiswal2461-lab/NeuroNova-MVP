"""SQLAlchemy model for stored Finding embeddings.

Layer 6 is the only layer that keeps a relational artefact backing its
on-disk reports. Each row holds:

  * the vector itself (pgvector ``vector(1536)`` — matches OpenAI
    ``text-embedding-3-small``),
  * a denormalised copy of the filter columns (``severity``,
    ``finding_type``, ``column_name``, ``confidence``) so the retriever
    can pre-narrow the candidate set with btree indexes *before* the
    expensive cosine-similarity sort,
  * the original ``embedded_text`` for traceability and debugging.

The actual :class:`agentic_engine.findings.models.Finding` content stays
on disk at ``data/findings/{dataset_id}.json``; this table only stores
what is needed to *retrieve* a finding, not the finding itself.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# Dimensionality of OpenAI text-embedding-3-small. Kept as a module-level
# constant so callers can sanity-check vectors before insertion.
EMBEDDING_DIMENSION: int = 1536


class FindingEmbedding(Base):
    """One row per (dataset_id, finding_id) embedding pair."""

    __tablename__ = "finding_embeddings"
    __table_args__ = (
        # A finding may only have a single live embedding per dataset.
        # Re-indexing replaces rows for a dataset wholesale.
        UniqueConstraint(
            "dataset_id", "finding_id",
            name="uq_finding_embeddings_dataset_id_finding_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )

    # Cascade delete tied to the dataset record — if the dataset is removed,
    # all its embeddings disappear in the same transaction.
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_records.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Logical reference to the Finding stored on disk; not an FK because
    # Findings are not relational.
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )

    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSION),
        nullable=False,
    )

    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    embedded_text: Mapped[str] = mapped_column(String, nullable=False)

    # Denormalised filter columns. Strings (not enums) so a new
    # FindingType/Severity does not require a migration to add it here.
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    finding_type: Mapped[str] = mapped_column(String(64), nullable=False)
    column_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
