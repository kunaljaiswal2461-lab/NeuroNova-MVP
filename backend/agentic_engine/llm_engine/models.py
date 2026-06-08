"""LLM Insight data models — the output of Layer 5.

An InsightReport is the human-language layer on top of the structured
FindingsReport. It contains three artefacts:

  * ExecutiveSummary       — narrative overview of the dataset
  * FindingExplanation[]   — per-finding plain-English explanation
  * SuggestedQuestion[]    — chat starters for the Conversational layer

The report is persisted to ``data/llm_cache/{dataset_id}.json`` and consumed
by the Streamlit AI Insight Center and (later) Layer 7's chat agent as
grounding context.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


# ── enums ────────────────────────────────────────────────────────────────────

class QuestionIntent(str, enum.Enum):
    """How the conversational layer should route a suggested question."""

    RAG = "RAG"                # answer from FindingsReport via vector retrieval
    QUERY = "QUERY"            # NL→Pandas direct dataset query
    EXPLORATION = "EXPLORATION"  # open-ended, may dispatch to either mode


# ── sub-models ───────────────────────────────────────────────────────────────

class ExecutiveSummary(BaseModel):
    """Top-level narrative for the AI Insight Center page."""

    headline: str
    overview: str
    key_strengths: list[str] = Field(default_factory=list)
    key_concerns: list[str] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)


class FindingExplanation(BaseModel):
    """Plain-English wrapper for a single Layer 3 Finding."""

    finding_id: uuid.UUID
    title: str
    plain_english: str
    why_it_matters: str
    suggested_action: str


class SuggestedQuestion(BaseModel):
    """A chat starter offered to the user on the Conversational Analyst page."""

    question: str
    intent: QuestionIntent
    target_columns: list[str] = Field(default_factory=list)
    rationale: str


class TokenUsage(BaseModel):
    """Aggregate token usage across all LLM calls for a single report."""

    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.call_count += 1


# ── top-level report ─────────────────────────────────────────────────────────

class InsightReport(BaseModel):
    """Output of the LLM Insight Layer."""

    dataset_id: uuid.UUID
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    executive_summary: ExecutiveSummary
    finding_explanations: list[FindingExplanation] = Field(default_factory=list)
    suggested_questions: list[SuggestedQuestion] = Field(default_factory=list)

    model_used: str
    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    # If the LLM provider is unavailable or unconfigured, the orchestrator
    # emits a structurally valid report with empty content so the pipeline
    # can still reach COMPLETE.
    degraded: Literal[True, False] = False
    degraded_reason: str | None = None

    @property
    def explanation_count(self) -> int:
        return len(self.finding_explanations)

    @property
    def question_count(self) -> int:
        return len(self.suggested_questions)

    def explanation_for(self, finding_id: uuid.UUID) -> FindingExplanation | None:
        for exp in self.finding_explanations:
            if exp.finding_id == finding_id:
                return exp
        return None
