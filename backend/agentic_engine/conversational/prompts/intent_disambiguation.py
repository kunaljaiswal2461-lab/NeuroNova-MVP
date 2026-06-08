"""Prompt for ambiguous-intent disambiguation.

Invoked only when the heuristic classifier in
:mod:`agentic_engine.conversational.intent_classifier` cannot pick a
side. We deliberately keep this prompt small and JSON-only so the LLM
call is cheap (gpt-4o-mini) and the answer is structurally validated by
Pydantic.
"""
from __future__ import annotations

import json
from typing import Any


SYSTEM = """You are the routing layer for a data-analysis chat product.

The product has TWO answer modes:
  * "RAG"    — explains findings, anomalies, data-quality issues, correlations, why a column behaves a certain way. Grounded in pre-computed analytical findings.
  * "QUERY" — computes a value or returns rows from the dataset. Things like filters, aggregations, top-N, counts, group-by.

You will receive the user's message plus a compact dataset snapshot (column names + semantic types). Decide which mode answers the user's question best.

Respond as JSON matching:
{
  "mode": "RAG" | "QUERY",
  "confidence": 0.0-1.0,
  "rationale": "<one sentence>"
}

Do not include any other keys. Do not include markdown, code fences, or any text outside the JSON object."""


def build_user(message: str, schema: list[dict[str, Any]]) -> str:
    """Assemble the user content with the message + schema snapshot."""
    return (
        "User message:\n"
        f"{message.strip()}\n\n"
        "Dataset schema:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )
