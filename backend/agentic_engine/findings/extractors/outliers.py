"""HIGH_OUTLIER_DENSITY extractor.

Uses the IQR-based outlier_pct computed by the quality profiler.
Severity tiers:
  HIGH   → outlier_pct >= 15%
  MEDIUM → outlier_pct >= 7%
  LOW    → outlier_pct >= 3%
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

_LOW_THRESHOLD = 3.0
_MEDIUM_THRESHOLD = 7.0
_HIGH_THRESHOLD = 15.0


def extract(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}
    stats_by_col = {s.name: s for s in report.stats.columns}

    for col_q in report.quality.columns:
        if col_q.outlier_pct is None or col_q.outlier_pct < _LOW_THRESHOLD:
            continue

        pct = col_q.outlier_pct
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

        num = stats_by_col.get(col_q.name, None)
        evidence: dict = {
            "outlier_pct": pct,
            "outlier_count": round(row_count * pct / 100),
            "row_count": row_count,
            "method": "IQR_1.5x",
        }
        if num and num.numeric:
            evidence.update({
                "p25": num.numeric.p25,
                "p75": num.numeric.p75,
                "min": num.numeric.min,
                "max": num.numeric.max,
                "mean": num.numeric.mean,
            })

        findings.append(
            Finding(
                type=FindingType.HIGH_OUTLIER_DENSITY,
                severity=severity,
                confidence=conf,
                column=col_q.name,
                title=f"High outlier density in '{col_q.name}' ({pct:.1f}% outliers)",
                description=(
                    f"{pct:.1f}% of non-null values in '{col_q.name}' fall outside "
                    f"the 1.5×IQR fence. "
                    f"Outliers of this density may indicate data entry errors, "
                    f"sensor noise, or a genuinely multi-modal distribution."
                ),
                evidence=evidence,
                semantic_context=semantic_by_col.get(col_q.name),
            )
        )

    return findings
