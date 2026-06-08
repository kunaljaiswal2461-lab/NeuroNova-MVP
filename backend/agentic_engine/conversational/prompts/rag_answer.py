
"""Prompt for RAG-mode answering.

The chat model is given:
  * the user message
  * the dataset overview (rows, cols, health grade, semantic-type roll-up)
  * the retrieved findings (title + description + severity + column +
    similarity), already pre-filtered and ranked by the Layer 6 retriever

It is asked to answer the user's question grounded in those findings,
citing the finding titles inline when relevant. The "no invented
statistics" rule is restated here so it is in-context for every turn,
not just enforced by the prompt builder upstream.
"""
from __future__ import annotations

import json
from typing import Any


SYSTEM = """You are a senior data analyst answering a user's question about a dataset.

Grounding rules (HARD):
  - Use ONLY the findings, dataset overview, and conversation history provided. Do not invent statistics, percentages, or column behaviours that are not in the context.
  - If the retrieved findings do not actually address the user's question, say so explicitly. Suggest a more specific question or that the user switch to direct-query mode.
  - When you cite specific numbers, they must come verbatim from a finding's title or description.
  - Cite findings inline by their numeric index: e.g. "[1]" or "[2, 3]".

Style:
  - Plain English. No markdown headings. No code blocks.
  - 2-4 short paragraphs. Lead with the answer, then the supporting evidence, then a brief recommended next step if relevant.
  - If the user asked a yes/no question, answer it in the first sentence.

The user's question follows in the next message."""


def build_user(
    message: str,
    *,
    overview: dict[str, Any],
    findings: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
) -> str:
    """Compose the user-side content for a RAG turn.

    Args:
        message: the raw user question.
        overview: compact dataset snapshot — rows, cols, health,
            semantic_types_summary, etc. Kept tiny on purpose so the
            findings dominate the context budget.
        findings: list of dicts with at least ``index``, ``title``,
            ``description``, ``severity``, ``column``, ``similarity``.
            Index is 1-based so the model's cite tags match what the UI
            renders.
        history: prior turns to preserve context flow. The streaming
            client already prepends history via its own pathway, so this
            argument is optional here.
    """
    pieces: list[str] = []

    pieces.append("Dataset overview:")
    pieces.append(json.dumps(overview, ensure_ascii=False, indent=2))
    pieces.append("")

    if findings:
        pieces.append("Retrieved findings (most relevant first):")
        for f in findings:
            line = (
                f"[{f['index']}] (severity={f['severity']}, "
                f"column={f.get('column') or '-'}, "
                f"similarity={f.get('similarity', 0.0):.2f}) "
                f"{f['title']}"
            )
            pieces.append(line)
            description = (f.get("description") or "").strip()
            if description:
                pieces.append(f"     {description}")
        pieces.append("")
    else:
        pieces.append(
            "No findings were retrieved for this question. Tell the user "
            "you cannot answer from the existing findings."
        )
        pieces.append("")

    if history:
        pieces.append("Recent conversation (oldest first):")
        for turn in history:
            role = turn.get("role", "user").upper()
            content = (turn.get("content") or "").strip()
            if content:
                pieces.append(f"  [{role}] {content}")
        pieces.append("")

    pieces.append("User question:")
    pieces.append(message.strip())
    return "\n".join(pieces)
