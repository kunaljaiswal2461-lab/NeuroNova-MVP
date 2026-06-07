"""Prompt for batched per-finding plain-English explanations.

One LLM call covers every HIGH/MEDIUM finding for the dataset — far cheaper
than emitting N separate calls.
"""
from __future__ import annotations

import json
from typing import Any


SYSTEM = """You are explaining statistical findings about a dataset to a non-technical reader.

You will receive a JSON array of structured Findings already extracted by the upstream profiler. Each finding has a stable `finding_id` (UUID), a type, a severity, a confidence score, an optional column, evidence (the raw numbers behind the observation), and an optional semantic tag.

For EACH finding, return a plain-English explanation. Do not invent findings, do not merge them, and do not omit any. Use the evidence values literally when quoting numbers.

Respond as JSON matching this schema:
{
  "explanations": [
    {
      "finding_id": "<the UUID from the input, copied verbatim>",
      "title": "<short headline, max 80 chars>",
      "plain_english": "<1-2 sentence explanation a product manager could read>",
      "why_it_matters": "<1 sentence on the downstream impact>",
      "suggested_action": "<1 sentence concrete recommendation>"
    },
    ...
  ]
}

The order of items in `explanations` must match the order of the input findings. Do not include markdown, code fences, or any text outside the JSON object."""


def build_user(findings_payload: list[dict[str, Any]]) -> str:
    """Assemble the user message from a list of pre-serialised findings."""
    return (
        "Explain the following findings. Output JSON with one "
        "`explanations` entry per input finding, in the same order:\n\n"
        f"{json.dumps(findings_payload, ensure_ascii=False, indent=2)}"
    )
