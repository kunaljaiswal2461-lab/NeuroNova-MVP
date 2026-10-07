"""Candidate protocol and outcome types (SPEC-03 §13.1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from evals.grader import ResultSet
    from evals.models import GoldenQuestion


@dataclass(frozen=True)
class LLMCall:
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


@dataclass(frozen=True)
class CandidateOutcome:
    kind: Literal["answer", "clarification", "refusal", "error"]
    result: "ResultSet | None" = None
    sql: str | None = None          # any SQL the system produced, even if it wasn't executed
    message: str | None = None      # clarification question / refusal reason
    error_code: str | None = None   # safe code only, never a raw driver message
    latency_ms: float = 0.0
    llm_calls: tuple[LLMCall, ...] = ()


@dataclass(frozen=True)
class EvalContext:
    """Everything a candidate may touch. ``fixture`` is a bootstrap.FixtureContext."""

    fixture: Any
    grader_connect: Any             # async factory -> asyncpg connection as nn_grader
    reference_clock: str
    reference_time_zone: str
    cassette_mode: Literal["off", "record", "replay"] = "replay"
    extras: dict = field(default_factory=dict)


class Candidate(Protocol):
    name: str

    async def answer(self, question: "GoldenQuestion", ctx: EvalContext) -> CandidateOutcome: ...
