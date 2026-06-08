"""SEMANTIC_TAG extractor.

Converts high-confidence semantic column classifications into findings.
These become the primary RAG context for questions about column meaning.

Only emits a finding when the semantic type is informative (not UNKNOWN or
the generic NUMERIC/TEXT fallbacks) and confidence meets the threshold.
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

_MIN_CONFIDENCE = 0.65

_UNINFORMATIVE_TYPES = {"UNKNOWN", "NUMERIC", "TEXT", "OTHER"}

_SEMANTIC_DESCRIPTIONS: dict[str, str] = {
    "IDENTIFIER": "a row identifier (primary key or surrogate key)",
    "FINANCIAL": "a financial metric (price, revenue, cost, or similar monetary value)",
    "GEOGRAPHIC": "a geographic attribute (location, region, coordinates, or postal code)",
    "TEMPORAL": "a temporal field (date, timestamp, or time-based value)",
    "CATEGORICAL": "a low-cardinality categorical variable",
    "BOOLEAN": "a boolean flag or binary indicator",
    "EMAIL": "an email address",
    "URL": "a URL or web address",
    "PHONE": "a phone number",
}


def extract(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count

    for sem in report.semantic.columns:
        if sem.semantic_type in _UNINFORMATIVE_TYPES:
            continue
        if sem.confidence < _MIN_CONFIDENCE:
            continue

        conf = confidence_score(
            row_count=row_count,
            effect=sem.confidence,
            min_effect=_MIN_CONFIDENCE,
            max_effect=1.0,
        )

        human_desc = _SEMANTIC_DESCRIPTIONS.get(
            sem.semantic_type,
            f"a {sem.semantic_type.lower().replace('_', ' ')} field",
        )

        findings.append(
            Finding(
                type=FindingType.SEMANTIC_TAG,
                severity=Severity.LOW,
                confidence=conf,
                column=sem.name,
                title=f"'{sem.name}' classified as {sem.semantic_type}",
                description=(
                    f"Column '{sem.name}' was automatically classified as {human_desc} "
                    f"with {sem.confidence:.0%} confidence. "
                    f"This classification informs downstream analysis, visualisation, "
                    f"and LLM grounding context."
                ),
                evidence={
                    "semantic_type": sem.semantic_type,
                    "classifier_confidence": sem.confidence,
                },
                semantic_context=sem.semantic_type,
            )
        )

    return findings
