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
    # SPEC-08 B9. Optional, so the candidates that predate them keep working.
    output_intent: dict | None = None
    notes: tuple[str, ...] = ()     # rewrites applied and intent warnings, as fixed codes
    # SPEC-09 W7. Optional, so every earlier candidate keeps working.
    presentation: dict | None = None        # the resolved pass-2 spec (answer text, display, caveats ...)
    repair_reason: str | None = None        # set when the one repair (or regenerate) happened
    pass2_fallback: str | None = None       # why pass 2 fell back to the templated summary, if it did
    pass2_ran: bool = False
    pass2_dropped: int = 0                  # sentences the number check removed
    first_attempt: "CandidateOutcome | None" = None     # what the turn would have been without the repair


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
