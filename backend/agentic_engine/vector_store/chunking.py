"""Convert Findings into embedding-ready text chunks.

The chunker is intentionally narrow: one :class:`FindingChunk` per
:class:`agentic_engine.findings.models.Finding`. We do **not** split a
finding across multiple vectors. Findings are already small, atomic
units of analytical signal, so splitting would only spread the meaning
thin across multiple low-similarity hits.

Embedding strategy (decided: title + description + column + semantic
context). We deliberately exclude the ``evidence`` dict because its raw
numbers (e.g. ``{"null_pct": 60.0}``) do not carry semantic signal for
natural-language queries and only inflate the embedded text length.
"""
from __future__ import annotations

from agentic_engine.findings.models import Finding, FindingsReport
from agentic_engine.vector_store.models import FindingChunk


# Hard cap to protect the embedder from pathologically long inputs.
# OpenAI ``text-embedding-3-small`` tops out at 8191 tokens (~32k chars);
# we cap well below that because findings should never be that verbose.
_MAX_TEXT_CHARS: int = 4000


def build_finding_text(finding: Finding) -> str:
    """Compose the natural-language string that will be embedded.

    Output shape: ``"<title>. <description> [column: X] [semantic: Y]"``.
    Empty fields are dropped; output is trimmed and capped at
    :data:`_MAX_TEXT_CHARS`.
    """
    parts: list[str] = [finding.title.strip()]

    description = finding.description.strip()
    if description:
        parts.append(description)

    if finding.column:
        parts.append(f"Column: {finding.column}")

    if finding.semantic_context:
        parts.append(f"Semantic type: {finding.semantic_context}")

    text = ". ".join(p for p in parts if p)
    return text[:_MAX_TEXT_CHARS]


def chunk_finding(finding: Finding) -> FindingChunk:
    """Build a single :class:`FindingChunk` from one Finding."""
    return FindingChunk(
        finding_id=finding.finding_id,
        text=build_finding_text(finding),
        severity=finding.severity,
        finding_type=finding.type,
        column=finding.column,
        confidence=finding.confidence,
    )


def chunk_findings(report: FindingsReport) -> list[FindingChunk]:
    """Convert every Finding in a report into a chunk, preserving order.

    Empty-text chunks are skipped — without text there is nothing to
    embed and pgvector would refuse a zero-length input anyway.
    """
    chunks: list[FindingChunk] = []
    for finding in report.findings:
        chunk = chunk_finding(finding)
        if not chunk.text:
            continue
        chunks.append(chunk)
    return chunks
