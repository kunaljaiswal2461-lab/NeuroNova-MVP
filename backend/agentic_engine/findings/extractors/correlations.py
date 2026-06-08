"""STRONG_CORRELATION and POTENTIAL_LEAKAGE extractors.

STRONG_CORRELATION — numeric pair whose |Pearson| or |Spearman| exceeds 0.8.
  HIGH   → |r| >= 0.95
  MEDIUM → |r| >= 0.85
  LOW    → |r| >= 0.70

POTENTIAL_LEAKAGE — a STRONG_CORRELATION pair where one column is a known
IDENTIFIER or TEMPORAL type, which can encode target information and cause
inflated model metrics.
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

_LOW_THRESHOLD = 0.70
_MEDIUM_THRESHOLD = 0.85
_HIGH_THRESHOLD = 0.95

_LEAKAGE_SEMANTIC_TYPES = {"IDENTIFIER", "TEMPORAL"}


def _severity(abs_r: float) -> Severity:
    if abs_r >= _HIGH_THRESHOLD:
        return Severity.HIGH
    if abs_r >= _MEDIUM_THRESHOLD:
        return Severity.MEDIUM
    return Severity.LOW


def extract(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count

    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    for corr in report.relationships.correlations:
        # Use the stronger of the two coefficients
        candidates = [
            ("pearson", corr.pearson),
            ("spearman", corr.spearman),
        ]
        best_method, best_r = max(
            ((m, r) for m, r in candidates if r is not None),
            key=lambda x: abs(x[1]),
            default=(None, None),
        )
        if best_r is None or abs(best_r) < _LOW_THRESHOLD:
            continue

        abs_r = abs(best_r)
        severity = _severity(abs_r)
        conf = confidence_score(
            row_count=row_count,
            effect=abs_r,
            min_effect=_LOW_THRESHOLD,
            max_effect=_HIGH_THRESHOLD,
        )

        direction = "positive" if best_r > 0 else "negative"
        findings.append(
            Finding(
                type=FindingType.STRONG_CORRELATION,
                severity=severity,
                confidence=conf,
                column=None,
                title=(
                    f"Strong {direction} correlation: '{corr.col_a}' ↔ '{corr.col_b}' "
                    f"({best_method} r={best_r:.3f})"
                ),
                description=(
                    f"'{corr.col_a}' and '{corr.col_b}' have a {direction} "
                    f"{best_method} correlation of {best_r:.3f}. "
                    f"Highly correlated features are candidates for dimensionality "
                    f"reduction or multicollinearity checks before modelling."
                ),
                evidence={
                    "col_a": corr.col_a,
                    "col_b": corr.col_b,
                    "pearson": corr.pearson,
                    "spearman": corr.spearman,
                    "best_method": best_method,
                    "best_r": best_r,
                    "row_count": row_count,
                },
                semantic_context=None,
            )
        )

        # Leakage check: one column is a temporal/identifier type
        sem_a = semantic_by_col.get(corr.col_a)
        sem_b = semantic_by_col.get(corr.col_b)
        leakage_col = None
        if sem_a in _LEAKAGE_SEMANTIC_TYPES:
            leakage_col = corr.col_a
        elif sem_b in _LEAKAGE_SEMANTIC_TYPES:
            leakage_col = corr.col_b

        if leakage_col and abs_r >= _MEDIUM_THRESHOLD:
            findings.append(
                Finding(
                    type=FindingType.POTENTIAL_LEAKAGE,
                    severity=Severity.HIGH,
                    confidence=round(conf * 0.85, 3),  # slightly discounted — heuristic
                    column=leakage_col,
                    title=(
                        f"Potential data leakage: '{leakage_col}' correlates strongly "
                        f"with '{corr.col_b if leakage_col == corr.col_a else corr.col_a}'"
                    ),
                    description=(
                        f"'{leakage_col}' is classified as a {sem_a or sem_b} column "
                        f"but has a high correlation ({best_r:.3f}) with another numeric "
                        f"column. Temporal and identifier columns can inadvertently encode "
                        f"target information, causing optimistic evaluation metrics."
                    ),
                    evidence={
                        "leakage_col": leakage_col,
                        "correlated_with": corr.col_b if leakage_col == corr.col_a else corr.col_a,
                        "correlation_r": best_r,
                        "leakage_semantic_type": sem_a if leakage_col == corr.col_a else sem_b,
                    },
                    semantic_context=sem_a if leakage_col == corr.col_a else sem_b,
                )
            )

    return findings
