"""Prompt for conversation-starter questions, routed by intent."""
from __future__ import annotations

import json
from typing import Any


SYSTEM = """You are designing the suggested-questions panel for a conversational data-analysis product.

You will receive a JSON snapshot of a dataset (schema, semantic tags, top correlations, key findings). Generate 5–7 questions a user might want to ask next.

Each question must be:
  - concrete and answerable from this specific dataset (reference real column names)
  - free of made-up statistics — the answer will be computed downstream, not produced by you

Each question must be tagged with an `intent`:
  - "RAG"         — answerable from the structured Findings (e.g. "Why does column X have many nulls?")
  - "QUERY"      — needs a direct dataset query / Pandas operation (e.g. "What is the average X by Y?")
  - "EXPLORATION" — open-ended, may dispatch to either mode

Respond as JSON matching this schema:
{
  "questions": [
    {
      "question": "<the question text>",
      "intent": "RAG" | "QUERY" | "EXPLORATION",
      "target_columns": ["<col>", ...],
      "rationale": "<1 sentence on why this is worth asking>"
    },
    ...
  ]
}

Aim for a mix of intents — do not return only one type. Do not include markdown, code fences, or any text outside the JSON object."""


def build_user(context: dict[str, Any]) -> str:
    """Assemble the user message from the shared dataset context."""
    return (
        "Here is the dataset snapshot. Propose 5-7 suggested questions:\n\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}"
    )
