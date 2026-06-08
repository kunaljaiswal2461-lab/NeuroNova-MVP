"""CONSTANT_COLUMN extractor.

A column with a single unique non-null value carries zero information and
should be flagged for removal. Always HIGH severity.
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport


def extract(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    for col_q in report.quality.columns:
        if not col_q.is_constant:
            continue

        # More rows → stronger evidence that it's genuinely constant, not a
        # coincidence of a tiny sample.
        conf = confidence_score(
            row_count=row_count,
            effect=row_count,
            min_effect=10,
            max_effect=1000,
        )

        findings.append(
            Finding(
                type=FindingType.CONSTANT_COLUMN,
                severity=Severity.HIGH,
                confidence=conf,
                column=col_q.name,
                title=f"Constant column '{col_q.name}' — zero variance",
                description=(
                    f"Column '{col_q.name}' contains only a single unique value "
                    f"across all {row_count:,} non-null rows. "
                    f"It provides no discriminative information and is a candidate "
                    f"for removal before modelling or analysis."
                ),
                evidence={
                    "is_constant": True,
                    "row_count": row_count,
                    "null_pct": col_q.null_pct,
                },
                semantic_context=semantic_by_col.get(col_q.name),
            )
        )

    return findings
