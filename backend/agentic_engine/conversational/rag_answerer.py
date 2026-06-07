"""RAG-mode answerer for Layer 7.

Glues the Layer 6 retriever to a streaming chat call. The retriever
gives us a ranked list of findings; we hand the LLM a compact prompt
containing the dataset overview plus the retrieved findings and stream
the response back as :class:`StreamDelta`s.

Two design invariants worth restating:

  * **No raw data in prompts** — the retriever returns Finding metadata
    only (titles + descriptions + severities + similarities), and that
    is all this module ever forwards. The prompt builder upstream owns
    this hard rule (Layer 5's ``prompt_builder.py``); we honour it here
    by composing the user message from the retriever output alone.
  * **Graceful degradation** — if the retriever returns an empty /
    degraded result, we still answer (truthfully: "no findings
    matched"). If the LLM is unavailable, we surface a single text
    delta carrying the failure message so the UI can render something
    sensible instead of an empty bubble.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from agentic_engine.conversational.models import RagCitation
from agentic_engine.conversational.prompts import rag_answer as rag_prompt
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable, StreamDelta
from agentic_engine.vector_store.embedder import Embedder
from agentic_engine.vector_store.models import RetrievalQuery, RetrievalResult
from agentic_engine.vector_store.retriever import retrieve as run_retrieve


logger = get_logger("conversational.rag")


# Token budget for the streamed answer. 800 tokens ≈ 600 words, which is
# more than enough for a multi-paragraph analyst-style reply.
_MAX_ANSWER_TOKENS: int = 800

# Retrieval defaults — chosen to give the prompt enough variety without
# blowing past the model's effective attention budget. ``min_similarity``
# is gentle on purpose; the prompt instructs the model to admit when
# retrieved findings do not actually address the question.
_DEFAULT_TOP_K: int = 6
_DEFAULT_MIN_SIMILARITY: float = 0.2


# ── public entry point ──────────────────────────────────────────────────────

async def stream_rag_answer(
    user_message: str,
    *,
    dataset_id: uuid.UUID,
    overview: dict[str, Any],
    history: list[dict[str, str]] | None,
    settings: Settings,
    llm: LLMClient | None,
    embedder: Embedder | None,
    db_session,  # AsyncSession — annotated loosely so this module doesn't pull SQLAlchemy
    top_k: int = _DEFAULT_TOP_K,
    min_similarity: float = _DEFAULT_MIN_SIMILARITY,
) -> AsyncIterator[tuple[list[RagCitation], StreamDelta]]:
    """Stream a RAG-grounded answer.

    Yields a sequence of ``(citations, delta)`` pairs. ``citations`` is
    the same list on every yield (a stable snapshot computed once before
    streaming starts) so the caller can emit a single ``citations``
    event up-front and ignore the field on subsequent deltas. Yielding
    citations alongside each delta is a convenience for callers that
    only need the citations at ``finished=True`` — they can read them
    from the terminal frame.

    Failure modes:
      * No LLM client → one terminal delta with a degraded message.
      * Retrieval degraded (no embedder, no index) → answer the user
        truthfully that no findings were available.
      * Stream open / mid-stream failure → one final delta carrying the
        error text in ``text`` and ``finished=True``.
    """
    # ── 1. Retrieve grounding findings. ───────────────────────────────────
    retrieval = await run_retrieve(
        dataset_id,
        RetrievalQuery(
            query=user_message,
            top_k=top_k,
            min_similarity=min_similarity,
        ),
        session=db_session,
        embedder=embedder,
        settings=settings,
    )
    citations = _retrieval_to_citations(retrieval)

    # ── 2. Bail out gracefully if there is no LLM. ────────────────────────
    if llm is None:
        text = (
            "The conversational layer is unavailable: no OpenAI API key is "
            "configured. Findings and visualisations remain accessible "
            "through the dataset endpoints."
        )
        yield citations, StreamDelta(text=text, finished=False)
        yield citations, StreamDelta(text="", finished=True, model="(unavailable)")
        return

    # ── 3. Build the prompt + stream the answer. ─────────────────────────
    findings_payload = _findings_payload(retrieval)
    user_content = rag_prompt.build_user(
        user_message,
        overview=overview,
        findings=findings_payload,
        history=history,
    )

    try:
        stream = llm.chat_stream(
            model=settings.openai_chat_model,
            system=rag_prompt.SYSTEM,
            user=user_content,
            history=history,
            max_tokens=_MAX_ANSWER_TOKENS,
            temperature=0.3,
        )
    except LLMUnavailable as exc:
        logger.warning("rag.stream_open_failed", error=str(exc))
        yield citations, StreamDelta(
            text=f"(answer unavailable: {exc})",
            finished=False,
        )
        yield citations, StreamDelta(text="", finished=True)
        return

    try:
        async for delta in stream:
            yield citations, delta
    except LLMUnavailable as exc:
        # Mid-stream failure — surface what we have plus an explanatory tail.
        logger.warning("rag.stream_failed_midway", error=str(exc))
        yield citations, StreamDelta(
            text=f"\n(stream interrupted: {exc})",
            finished=False,
        )
        yield citations, StreamDelta(text="", finished=True)


# ── helpers ─────────────────────────────────────────────────────────────────

def _retrieval_to_citations(retrieval: RetrievalResult) -> list[RagCitation]:
    """Convert retriever hits into UI-ready citations.

    The retriever's hits already include enough metadata for the chat
    UI; we just rename ``embedded_text`` → ``title`` since that's how
    the citation chip is labelled.
    """
    citations: list[RagCitation] = []
    for hit in retrieval.hits:
        try:
            citations.append(
                RagCitation(
                    finding_id=hit.finding_id,
                    similarity=hit.similarity,
                    title=hit.embedded_text,
                    severity=Severity(hit.severity)
                    if not isinstance(hit.severity, Severity)
                    else hit.severity,
                    finding_type=FindingType(hit.finding_type)
                    if not isinstance(hit.finding_type, FindingType)
                    else hit.finding_type,
                    column=hit.column,
                )
            )
        except ValueError as exc:
            # A stored severity / finding_type string that no longer maps
            # to a known enum value — log and skip rather than tank the
            # whole turn.
            logger.warning(
                "rag.citation_enum_mismatch",
                finding_id=str(hit.finding_id),
                severity=hit.severity,
                finding_type=hit.finding_type,
                error=str(exc),
            )
    return citations


def _findings_payload(retrieval: RetrievalResult) -> list[dict[str, Any]]:
    """Shape the retriever hits into the dict structure
    :func:`rag_prompt.build_user` expects."""
    payload: list[dict[str, Any]] = []
    for index, hit in enumerate(retrieval.hits, start=1):
        payload.append({
            "index": index,
            "title": hit.embedded_text,
            "description": "",  # the embedded_text already carries the
                                 # title+description blob produced by the
                                 # Layer 6 chunker; no extra text on hand.
            "severity": (
                hit.severity.value
                if isinstance(hit.severity, Severity)
                else hit.severity
            ),
            "column": hit.column,
            "similarity": hit.similarity,
        })
    return payload
