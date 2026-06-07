"""DUPLICATE_ROWS extractor.

Flags datasets where a non-trivial fraction of rows are exact duplicates.
Severity tiers:
  HIGH   → dup_pct >= 20%
  MEDIUM → dup_pct >= 5%
  LOW    → dup_pct >= 1%
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

_LOW_THRESHOLD = 1.0
_MEDIUM_THRESHOLD = 5.0
_HIGH_THRESHOLD = 20.0


def extract(report: ProfileReport) -> list[Finding]:
    pct = report.quality.duplicate_row_pct
    if pct < _LOW_THRESHOLD:
        return []

    row_count = report.schema_.row_count
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

    dup_count = round(row_count * pct / 100)
    return [
        Finding(
            type=FindingType.DUPLICATE_ROWS,
            severity=severity,
            confidence=conf,
            column=None,
            title=f"Duplicate rows detected ({pct:.1f}% of dataset)",
            description=(
                f"{pct:.1f}% of rows ({dup_count:,} of {row_count:,}) are exact "
                f"duplicates. Duplicates inflate row counts, skew summary statistics, "
                f"and can cause data leakage between train and test splits."
            ),
            evidence={
                "duplicate_row_pct": pct,
                "duplicate_count": dup_count,
                "row_count": row_count,
            },
        )
    ]
