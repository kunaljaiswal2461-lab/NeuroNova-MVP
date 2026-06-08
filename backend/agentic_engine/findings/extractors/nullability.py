
"""HIGH_NULLABILITY extractor.

Emits one finding per column whose null percentage crosses the LOW threshold.
Severity tiers:
  HIGH   → null_pct >= 50%
  MEDIUM → null_pct >= 20%
  LOW    → null_pct >= 5%
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

_LOW_THRESHOLD = 5.0
_MEDIUM_THRESHOLD = 20.0
_HIGH_THRESHOLD = 50.0


def extract(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count

    quality_by_col = {q.name: q for q in report.quality.columns}
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    for col_q in report.quality.columns:
        pct = col_q.null_pct
        if pct < _LOW_THRESHOLD:
            continue

        if pct >= _HIGH_THRESHOLD:
            severity = Severity.HIGH
        elif pct >= _MEDIUM_THRESHOLD:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        conf = confidence_score(
            row_count=row_count,
            effect=pct,
            min_effect=_LOW_THRESHOLD,
            max_effect=_HIGH_THRESHOLD,
        )

        null_count = round(row_count * pct / 100)
        semantic = semantic_by_col.get(col_q.name)

        findings.append(
            Finding(
                type=FindingType.HIGH_NULLABILITY,
                severity=severity,
                confidence=conf,
                column=col_q.name,
                title=f"High null rate in '{col_q.name}' ({pct:.1f}%)",
                description=(
                    f"Column '{col_q.name}' has {pct:.1f}% missing values "
                    f"({null_count:,} of {row_count:,} rows). "
                    f"This may indicate data collection gaps or an optional field "
                    f"that is rarely populated."
                ),
                evidence={
                    "null_pct": pct,
                    "null_count": null_count,
                    "row_count": row_count,
                    "threshold_pct": _LOW_THRESHOLD,
                },
                semantic_context=semantic,
            )
        )

    return findings
