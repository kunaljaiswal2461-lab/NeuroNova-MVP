"""SKEWED_DISTRIBUTION and IMBALANCED_DISTRIBUTION extractors.

SKEWED_DISTRIBUTION — numeric columns with |skew| above threshold.
  HIGH   → |skew| >= 3.0
  MEDIUM → |skew| >= 1.5
  LOW    → |skew| >= 0.8

IMBALANCED_DISTRIBUTION — categorical columns where the dominant value
accounts for a disproportionate share of non-null rows.
  HIGH   → top value >= 90%
  MEDIUM → top value >= 75%
  LOW    → top value >= 60%
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

_SKEW_LOW = 0.8
_SKEW_MEDIUM = 1.5
_SKEW_HIGH = 3.0

_IMBAL_LOW = 60.0
_IMBAL_MEDIUM = 75.0
_IMBAL_HIGH = 90.0


def _skew_findings(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    for col_stats in report.stats.columns:
        num = col_stats.numeric
        if num is None or num.skew is None:
            continue
        abs_skew = abs(num.skew)
        if abs_skew < _SKEW_LOW:
            continue

        if abs_skew >= _SKEW_HIGH:
            severity = Severity.HIGH
        elif abs_skew >= _SKEW_MEDIUM:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        conf = confidence_score(
            row_count=row_count,
            effect=abs_skew,
            min_effect=_SKEW_LOW,
            max_effect=_SKEW_HIGH,
        )
        direction = "right (positive)" if num.skew > 0 else "left (negative)"
        findings.append(
            Finding(
                type=FindingType.SKEWED_DISTRIBUTION,
                severity=severity,
                confidence=conf,
                column=col_stats.name,
                title=f"Skewed distribution in '{col_stats.name}' (skew={num.skew:.2f})",
                description=(
                    f"'{col_stats.name}' is skewed {direction} with skewness {num.skew:.2f}. "
                    f"This indicates a long tail and may require log or power transformation "
                    f"before use in models that assume normality."
                ),
                evidence={
                    "skew": num.skew,
                    "kurtosis": num.kurtosis,
                    "mean": num.mean,
                    "median": num.median,
                    "std": num.std,
                    "min": num.min,
                    "max": num.max,
                },
                semantic_context=semantic_by_col.get(col_stats.name),
            )
        )

    return findings


def _imbalanced_findings(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    for col_stats in report.stats.columns:
        cat = col_stats.categorical
        if cat is None or not cat.top_values or cat.count == 0:
            continue

        top_label, top_count = cat.top_values[0]
        top_pct = 100.0 * top_count / cat.count

        if top_pct < _IMBAL_LOW:
            continue

        if top_pct >= _IMBAL_HIGH:
            severity = Severity.HIGH
        elif top_pct >= _IMBAL_MEDIUM:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        conf = confidence_score(
            row_count=row_count,
            effect=top_pct,
            min_effect=_IMBAL_LOW,
            max_effect=_IMBAL_HIGH,
        )
        findings.append(
            Finding(
                type=FindingType.IMBALANCED_DISTRIBUTION,
                severity=severity,
                confidence=conf,
                column=col_stats.name,
                title=(
                    f"Imbalanced distribution in '{col_stats.name}' "
                    f"— '{top_label}' is {top_pct:.1f}% of values"
                ),
                description=(
                    f"The dominant value '{top_label}' accounts for {top_pct:.1f}% "
                    f"of non-null rows in '{col_stats.name}'. "
                    f"Severe imbalance can bias classifiers and mislead aggregation metrics."
                ),
                evidence={
                    "dominant_value": top_label,
                    "dominant_pct": round(top_pct, 2),
                    "dominant_count": top_count,
                    "cardinality": cat.cardinality,
                    "non_null_count": cat.count,
                    "top_values": cat.top_values[:5],
                },
                semantic_context=semantic_by_col.get(col_stats.name),
            )
        )

    return findings


def extract(report: ProfileReport) -> list[Finding]:
    return _skew_findings(report) + _imbalanced_findings(report)
