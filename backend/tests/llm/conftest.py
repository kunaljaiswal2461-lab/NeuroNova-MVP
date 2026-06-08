"""Shared fixtures for Layer 5 unit tests.

We build a single, realistic (ProfileReport, FindingsReport) pair and pair
it with a configurable ``FakeLLM`` that records every call and returns
canned JSON responses. The FakeLLM is structurally compatible with
``agentic_engine.llm_engine.base_llm.LLMClient``.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from app.core.config import Settings
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding, FindingsReport
from agentic_engine.llm_engine.base_llm import ChatResult, LLMUnavailable
from agentic_engine.profiler.report import (
    ColumnQuality,
    ColumnStats,
    Correlation,
    HealthSection,
    NumericStats,
    CategoricalStats,
    ProfileReport,
    QualitySection,
    RelationshipSection,
    SchemaColumn,
    SchemaSection,
    SemanticSection,
    SemanticTag,
    StatsSection,
)


# ── FakeLLM ──────────────────────────────────────────────────────────────────

@dataclass
class _RecordedCall:
    model: str
    system: str
    user: str
    max_tokens: int
    temperature: float


@dataclass
class FakeLLM:
    """Records every call and returns the next response from a queue.

    Each entry in ``responses`` is either a dict (returned as JSON) or an
    Exception (raised). Responses are consumed in FIFO order, one per call.
    """

    responses: list[Any] = field(default_factory=list)
    calls: list[_RecordedCall] = field(default_factory=list)
    input_tokens_per_call: int = 250
    output_tokens_per_call: int = 400

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> ChatResult:
        self.calls.append(
            _RecordedCall(
                model=model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
        if not self.responses:
            raise AssertionError("FakeLLM ran out of canned responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return ChatResult(
            content_json=nxt,
            model=model,
            input_tokens=self.input_tokens_per_call,
            output_tokens=self.output_tokens_per_call,
        )


# ── canned response builders ─────────────────────────────────────────────────

def make_summary_response() -> dict:
    return {
        "headline": "Sales dataset with notable revenue skew and high-quality customer columns.",
        "overview": (
            "The dataset is moderately healthy with a few important data-quality gaps. "
            "Revenue is heavily right-skewed and contains outliers, while age and country "
            "are clean. Strong correlation between price and revenue suggests potential "
            "leakage if both are used as features."
        ),
        "key_strengths": [
            "Customer columns (age, country) have no nulls",
            "Schema is well-typed",
        ],
        "key_concerns": [
            "Revenue has 12% outliers and right-skewed",
            "price↔revenue correlation is 0.97",
        ],
        "recommended_next_steps": [
            "Investigate revenue outliers",
            "Drop one of price/revenue before modelling",
        ],
    }


def make_explanations_response(finding_ids: list[str]) -> dict:
    return {
        "explanations": [
            {
                "finding_id": fid,
                "title": f"Plain-English title #{i}",
                "plain_english": f"Explanation for finding {fid}.",
                "why_it_matters": "It affects downstream modelling.",
                "suggested_action": "Investigate and clean the column.",
            }
            for i, fid in enumerate(finding_ids)
        ]
    }


def make_questions_response() -> dict:
    return {
        "questions": [
            {
                "question": "Which products contribute most to revenue outliers?",
                "intent": "QUERY",
                "target_columns": ["revenue", "product"],
                "rationale": "Outliers may be concentrated in a few products.",
            },
            {
                "question": "Why does the price column correlate so strongly with revenue?",
                "intent": "RAG",
                "target_columns": ["price", "revenue"],
                "rationale": "The Findings layer flagged potential leakage.",
            },
            {
                "question": "Are there segments where the dataset is sparse?",
                "intent": "EXPLORATION",
                "target_columns": ["country", "age"],
                "rationale": "Coverage gaps may bias downstream models.",
            },
        ]
    }


# ── ProfileReport + FindingsReport fixtures ─────────────────────────────────

@pytest.fixture
def dataset_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def profile_report(dataset_id) -> ProfileReport:
    return ProfileReport(
        dataset_id=dataset_id,
        generated_at=datetime.now(timezone.utc),
        schema=SchemaSection(
            row_count=2000,
            col_count=4,
            columns=[
                SchemaColumn(name="age", dtype="Int64", polars_dtype="Int64", nullable=True),
                SchemaColumn(name="country", dtype="Utf8", polars_dtype="Utf8", nullable=True),
                SchemaColumn(name="price", dtype="Float64", polars_dtype="Float64", nullable=True),
                SchemaColumn(name="revenue", dtype="Float64", polars_dtype="Float64", nullable=True),
            ],
        ),
        stats=StatsSection(columns=[
            ColumnStats(
                name="age", inferred_kind="numeric",
                numeric=NumericStats(count=2000, mean=35.0, median=34.0,
                                     min=18.0, max=65.0, skew=0.2),
            ),
            ColumnStats(
                name="country", inferred_kind="categorical",
                categorical=CategoricalStats(
                    count=2000, cardinality=5, mode="US",
                    top_values=[("US", 1500), ("UK", 250), ("DE", 150), ("FR", 80), ("AU", 20)],
                ),
            ),
            ColumnStats(
                name="price", inferred_kind="numeric",
                numeric=NumericStats(count=2000, mean=100.0, median=95.0,
                                     min=10.0, max=500.0, skew=1.1),
            ),
            ColumnStats(
                name="revenue", inferred_kind="numeric",
                numeric=NumericStats(count=2000, mean=550.0, median=420.0,
                                     min=20.0, max=8000.0, skew=3.4),
            ),
        ]),
        quality=QualitySection(
            duplicate_row_pct=2.5,
            columns=[
                ColumnQuality(name="age", null_pct=0.0, is_constant=False, outlier_pct=1.0),
                ColumnQuality(name="country", null_pct=0.0, is_constant=False),
                ColumnQuality(name="price", null_pct=0.0, is_constant=False, outlier_pct=4.0),
                ColumnQuality(name="revenue", null_pct=0.0, is_constant=False, outlier_pct=12.0),
            ],
        ),
        relationships=RelationshipSection(correlations=[
            Correlation(col_a="price", col_b="revenue", pearson=0.97, spearman=0.95),
            Correlation(col_a="age", col_b="price", pearson=0.05, spearman=0.04),
        ]),
        semantic=SemanticSection(columns=[
            SemanticTag(name="age", semantic_type="NUMERIC", confidence=0.6),
            SemanticTag(name="country", semantic_type="GEOGRAPHIC", confidence=0.9),
            SemanticTag(name="price", semantic_type="FINANCIAL", confidence=0.85),
            SemanticTag(name="revenue", semantic_type="FINANCIAL", confidence=0.85),
        ]),
        health=HealthSection(score=78.0, grade="B", components={}),
    )


@pytest.fixture
def findings_report(dataset_id) -> FindingsReport:
    return FindingsReport(
        dataset_id=dataset_id,
        findings=[
            Finding(
                finding_id=uuid.uuid4(),
                type=FindingType.SKEWED_DISTRIBUTION, severity=Severity.HIGH,
                confidence=0.88, column="revenue",
                title="Right-skewed: revenue",
                description="revenue is heavily right-skewed (skew=3.4)",
                evidence={"skew": 3.4},
            ),
            Finding(
                finding_id=uuid.uuid4(),
                type=FindingType.HIGH_OUTLIER_DENSITY, severity=Severity.HIGH,
                confidence=0.82, column="revenue",
                title="Outliers: revenue",
                description="12% outliers in revenue",
                evidence={"outlier_pct": 12.0},
            ),
            Finding(
                finding_id=uuid.uuid4(),
                type=FindingType.STRONG_CORRELATION, severity=Severity.MEDIUM,
                confidence=0.95, column=None,
                title="Strong correlation price ↔ revenue",
                description="pearson=0.97",
                evidence={
                    "col_a": "price", "col_b": "revenue",
                    "pearson": 0.97, "spearman": 0.95,
                    "best_method": "pearson", "best_r": 0.97,
                },
            ),
            Finding(
                finding_id=uuid.uuid4(),
                type=FindingType.SEMANTIC_TAG, severity=Severity.LOW,
                confidence=0.85, column="country",
                title="country: GEOGRAPHIC",
                description="",
                evidence={"semantic_type": "GEOGRAPHIC"},
                semantic_context="GEOGRAPHIC",
            ),
        ],
    )


@pytest.fixture
def settings_no_key(tmp_path) -> Settings:
    """Settings with no OpenAI key — used to trigger degraded mode."""
    return Settings(
        api_key="test-api-key-12345",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        openai_api_key="",
        data_dir=tmp_path,
    )


@pytest.fixture
def settings_with_key(tmp_path) -> Settings:
    return Settings(
        api_key="test-api-key-12345",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        openai_api_key="sk-test-fake-key",
        data_dir=tmp_path,
    )


# Re-export so tests can import without touching internals.
__all__ = [
    "FakeLLM",
    "LLMUnavailable",
    "make_summary_response",
    "make_explanations_response",
    "make_questions_response",
]
