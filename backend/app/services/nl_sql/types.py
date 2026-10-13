"""Result types of NL->SQL pass 1 (SPEC-08 B1)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

OUTPUT_TYPES: tuple[str, ...] = (
    "kpi", "kpi_comparison", "table", "ranked_list", "bar", "grouped_bar", "line", "multi_line",
    "area", "pie", "scatter", "histogram", "heatmap", "pivot", "narrative", "takeaways",
)
REASON_CODES: tuple[str, ...] = ("write_request", "not_in_schema", "out_of_scope", "other")

# Every code a Pass1Result can carry in ``failure_code``.
FAILURE_CODES = frozenset({
    "empty_question", "question_too_long", "empty_scope", "over_budget", "catalog_unavailable",
    "source_unavailable", "llm_unavailable", "envelope_invalid", "canary_leak", "instruction_leak",
})


@dataclass(frozen=True)
class Turn:
    """One earlier exchange, passed back as context (at most the last 3 are used). Never carries rows."""

    question: str
    sql: str | None = None
    assumptions: tuple[str, ...] = ()
    output_intent: dict | None = None
    outcome: Literal["answer", "clarification", "unsupported", "failed"] = "answer"


@dataclass(frozen=True)
class LLMCall:
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class Encodings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: str | None
    y: list[str]
    series: str | None
    label: str | None
    value: str | None


class OutputIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal[*OUTPUT_TYPES]
    encodings: Encodings


class Envelope(BaseModel):
    """Exactly one of three kinds. Per-kind rules are enforced by ``envelope.parse_envelope``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["sql", "clarification", "unsupported"]
    sql: str | None
    assumptions: list[str]
    tables_used: list[str]
    output_intent: OutputIntent | None
    clarification: str | None
    reason_code: Literal[*REASON_CODES] | None
    reason: str | None


@dataclass(frozen=True)
class Pass1Result:
    envelope: Envelope | None
    failure_code: str | None = None
    snapshot_id: UUID | None = None
    schema_fingerprint: str | None = None
    prompt_version: str | None = None
    llm_call: LLMCall | None = None
    rewrites: tuple[str, ...] = ()
    # Fixed sub-code of an ``envelope_invalid``: length | refusal | invalid_json | not_valid (SPEC-09 C5).
    failure_detail: str | None = None
