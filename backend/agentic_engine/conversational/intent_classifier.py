"""Intent routing for the conversational chat layer.

Decides whether a user message should be answered via the RAG path
(grounded in Layer 6 finding embeddings) or the QUERY path
(NL→Polars expression executed against the on-disk dataset).

Strategy — cheapest-first cascade:

  1. **Session override** — if the session was created with
     ``mode=RAG`` or ``mode=QUERY``, that always wins. The classifier
     never runs and no LLM call is made.
  2. **Heuristic** — tokenise the message and score it against a small
     signal lexicon. If one side wins decisively (margin ≥ 1 signal),
     return immediately with a confidence proportional to the margin.
  3. **LLM fallback** — only when the heuristic is ambiguous *and* the
     LLM client is configured. Single small JSON call to gpt-4o-mini.
  4. **Final default** — if no LLM is available, fall back to RAG with
     low confidence. RAG is the safer default: it never executes code
     against the dataset, only retrieves text.

Returns a structured :class:`IntentDecision` carrying provenance so the
UI can show which path was taken and why.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from agentic_engine.conversational.models import (
    ConversationMode,
    IntentDecision,
    IntentLabel,
    IntentRoutedBy,
)
from agentic_engine.conversational.prompts import intent_disambiguation as prompt
from agentic_engine.llm_engine.base_llm import LLMClient, LLMUnavailable


logger = get_logger("conversational.intent")


# ── lexicon ──────────────────────────────────────────────────────────────────

# QUERY signals: imperative compute / list / filter / aggregate verbs and
# operators. These tend to imply "go look at the rows and give me a
# number or a slice".
_QUERY_SIGNALS: frozenset[str] = frozenset({
    "show", "list", "filter", "where", "select", "fetch", "return",
    "count", "how many", "how much",
    "average", "mean", "median", "sum", "min", "max", "total",
    "group by", "groupby", "per", "by",
    "top", "bottom", "first", "last", "limit",
    "rows where", "records where",
    "between",
    ">", "<", ">=", "<=", "==", "!=",
})

# RAG signals: explanation / reasoning / quality language. These imply
# "help me understand the dataset" rather than "compute X".
_RAG_SIGNALS: frozenset[str] = frozenset({
    "why", "explain", "explanation",
    "what does", "what do",
    "analyze", "analyse",
    "describe", "tell me about",
    "recommend", "recommendation", "suggest", "suggestion",
    "insight", "insights",
    "issue", "issues", "problem", "problems", "concern", "concerns",
    "quality", "anomaly", "anomalies", "outlier", "outliers",
    "correlation", "correlated", "relationship", "pattern",
    "skew", "skewed", "distribution",
    "missing", "nulls", "null rate",
})

# Confidence floor for a heuristic decision — chosen so a single
# unambiguous signal (e.g. user says "show me rows where x > 5") gets
# meaningful weight without ever claiming certainty.
_HEURISTIC_BASE_CONFIDENCE: float = 0.55

# Confidence ceiling for a heuristic decision; we always leave headroom
# so the LLM path can express higher certainty when invoked.
_HEURISTIC_CONFIDENCE_CEILING: float = 0.85

# When the LLM fallback is unavailable and the heuristic is ambiguous,
# default to RAG with this low confidence. RAG is the safer fallback
# because it never executes code.
_LLM_UNAVAILABLE_FALLBACK_CONFIDENCE: float = 0.45


# ── public entry point ──────────────────────────────────────────────────────

async def classify_intent(
    message: str,
    *,
    session_mode: ConversationMode,
    schema_snapshot: list[dict[str, Any]] | None,
    llm: LLMClient | None,
    settings: Settings,
) -> IntentDecision:
    """Pick a chat mode for a single user message.

    Args:
        message: the raw user text.
        session_mode: the mode the session was created with. ``AUTO``
            invokes the classifier; ``RAG``/``QUERY`` short-circuit it.
        schema_snapshot: an optional compact list of
            ``{name, dtype, semantic_type}`` dicts used as grounding
            context for the LLM fallback. Ignored if the heuristic
            already settled it.
        llm: optional :class:`LLMClient` for the disambiguation call.
            When ``None`` and the heuristic is ambiguous, the classifier
            falls back to RAG with low confidence.
        settings: app settings (used to pick the disambiguation model).
    """
    # ── 1. Session-level override wins outright. ──────────────────────────
    if session_mode is ConversationMode.RAG:
        return IntentDecision(
            mode="RAG",
            confidence=1.0,
            rationale="session pinned to RAG mode by user",
            routed_by=IntentRoutedBy.SESSION_OVERRIDE,
        )
    if session_mode is ConversationMode.QUERY:
        return IntentDecision(
            mode="QUERY",
            confidence=1.0,
            rationale="session pinned to QUERY mode by user",
            routed_by=IntentRoutedBy.SESSION_OVERRIDE,
        )

    # ── 2. Cheap heuristic on the bag-of-tokens. ──────────────────────────
    label, confidence, rationale = _classify_heuristic(message)
    if label is not IntentLabel.AMBIGUOUS:
        return IntentDecision(
            mode=label.value,
            confidence=confidence,
            rationale=rationale,
            routed_by=IntentRoutedBy.HEURISTIC,
        )

    # ── 3. LLM fallback (single JSON call). ───────────────────────────────
    if llm is not None:
        try:
            decision = await _classify_with_llm(
                message,
                schema_snapshot=schema_snapshot or [],
                llm=llm,
                settings=settings,
            )
            return decision
        except LLMUnavailable as exc:
            logger.warning(
                "intent.llm_fallback_unavailable",
                error=str(exc),
            )
            # Fall through to the final default.

    # ── 4. Safe default — RAG, low confidence. ────────────────────────────
    logger.info("intent.default_to_rag", message_len=len(message))
    return IntentDecision(
        mode="RAG",
        confidence=_LLM_UNAVAILABLE_FALLBACK_CONFIDENCE,
        rationale=(
            "ambiguous message and no LLM client configured for "
            "disambiguation; defaulting to RAG (safer, no code execution)"
        ),
        routed_by=IntentRoutedBy.HEURISTIC,
    )


# ── heuristic ────────────────────────────────────────────────────────────────

# Words split on any non-alphanumeric character so phrases like
# "groupby" and "rows where" can still be matched as multi-word signals
# via substring scan.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


def _classify_heuristic(message: str) -> tuple[IntentLabel, float, str]:
    """Score a message against the signal lexicons.

    Multi-word signals (e.g. "rows where") are matched as substrings of
    the lower-cased message; single-word signals match on token equality
    so a column name like ``count_per_user`` doesn't trigger a false
    "count" hit on its own.

    Returns ``(label, confidence, rationale)``.
    """
    lowered = message.lower().strip()
    if not lowered:
        return IntentLabel.AMBIGUOUS, 0.0, "empty message"

    tokens = set(_TOKEN_RE.findall(lowered))

    query_hits = _count_signals(lowered, tokens, _QUERY_SIGNALS)
    rag_hits = _count_signals(lowered, tokens, _RAG_SIGNALS)

    if query_hits == 0 and rag_hits == 0:
        return IntentLabel.AMBIGUOUS, 0.0, "no routing signals matched"

    if query_hits == rag_hits:
        return (
            IntentLabel.AMBIGUOUS,
            0.0,
            f"tied signal counts (rag={rag_hits}, query={query_hits})",
        )

    if query_hits > rag_hits:
        margin = query_hits - rag_hits
        return (
            IntentLabel.QUERY,
            _confidence_from_margin(margin),
            f"query signals dominated (rag={rag_hits}, query={query_hits})",
        )

    margin = rag_hits - query_hits
    return (
        IntentLabel.RAG,
        _confidence_from_margin(margin),
        f"rag signals dominated (rag={rag_hits}, query={query_hits})",
    )


def _count_signals(
    lowered_message: str,
    tokens: set[str],
    signals: Iterable[str],
) -> int:
    """Count how many ``signals`` fired in the message.

    Multi-word signals (containing whitespace or non-word chars) are
    matched as substrings; single-word signals must match a whole token
    so accidental substring overlap doesn't inflate the count.
    """
    count = 0
    for signal in signals:
        if any(not c.isalnum() and c != "_" for c in signal):
            # Multi-word / operator signal — substring match is correct.
            if signal in lowered_message:
                count += 1
        else:
            # Single-word signal — match against the tokenised set so a
            # column called ``total_count`` doesn't trigger "count".
            if signal in tokens:
                count += 1
    return count


def _confidence_from_margin(margin: int) -> float:
    """Map a signal-margin to a [_BASE_, _CEILING_] confidence score.

    Margin of 1 → base confidence; each extra signal adds 0.1, capped
    at the ceiling. Keeps the heuristic from ever claiming >ceiling so
    the LLM path can always express higher certainty.
    """
    raw = _HEURISTIC_BASE_CONFIDENCE + 0.1 * max(0, margin - 1)
    return min(_HEURISTIC_CONFIDENCE_CEILING, raw)


# ── LLM fallback ─────────────────────────────────────────────────────────────

async def _classify_with_llm(
    message: str,
    *,
    schema_snapshot: list[dict[str, Any]],
    llm: LLMClient,
    settings: Settings,
) -> IntentDecision:
    """Single JSON-mode call against the mini chat model.

    Parses the response with Pydantic; on validation failure raises
    :class:`LLMUnavailable` so the caller can fall back cleanly.
    """
    result = await llm.chat_json(
        model=settings.openai_mini_model,
        system=prompt.SYSTEM,
        user=prompt.build_user(message, schema_snapshot),
        # Tight token budget — JSON object is a single line of ~4 keys.
        max_tokens=200,
        temperature=0.0,
    )

    payload = result.content_json
    try:
        mode = payload["mode"]
        if mode not in ("RAG", "QUERY"):
            raise ValueError(f"unknown mode: {mode!r}")
        confidence = float(payload.get("confidence", 0.6))
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(payload.get("rationale", "(no rationale)"))[:500]
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        logger.warning(
            "intent.llm_response_invalid",
            payload=payload,
            error=str(exc),
        )
        raise LLMUnavailable(f"intent classifier returned bad JSON: {exc}") from exc

    return IntentDecision(
        mode=mode,
        confidence=confidence,
        rationale=rationale,
        routed_by=IntentRoutedBy.LLM,
    )
