"""ID_COLUMN_DETECTED extractor.

Identifies columns that are likely row identifiers — semantically tagged as
IDENTIFIER, or numeric/string columns whose cardinality ≈ row count.
These columns should typically be excluded from feature engineering.
"""
from __future__ import annotations

from agentic_engine.findings.confidence import confidence_score
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

_CARDINALITY_RATIO_THRESHOLD = 0.95  # unique values / row_count


def extract(report: ProfileReport) -> list[Finding]:
    findings: list[Finding] = []
    row_count = report.schema_.row_count
    if row_count == 0:
        return findings

    semantic_by_col = {s.name: s for s in report.semantic.columns}
    stats_by_col = {s.name: s for s in report.stats.columns}

    flagged: set[str] = set()

    # Semantic route: column explicitly classified IDENTIFIER
    for sem in report.semantic.columns:
        if sem.semantic_type == "IDENTIFIER":
            conf = confidence_score(
                row_count=row_count,
                effect=sem.confidence,
                min_effect=0.5,
                max_effect=1.0,
            )
            findings.append(
                Finding(
                    type=FindingType.ID_COLUMN_DETECTED,
                    severity=Severity.LOW,
                    confidence=conf,
                    column=sem.name,
                    title=f"Identifier column detected: '{sem.name}'",
                    description=(
                        f"'{sem.name}' appears to be a row identifier "
                        f"(semantic confidence {sem.confidence:.0%}). "
                        f"Identifier columns should be excluded from statistical "
                        f"modelling to avoid spurious correlations."
                    ),
                    evidence={
                        "semantic_type": sem.semantic_type,
                        "semantic_confidence": sem.confidence,
                        "detection_method": "semantic_name_match",
                    },
                    semantic_context=sem.semantic_type,
                )
            )
            flagged.add(sem.name)

    # Structural route: near-unique cardinality on categorical/text columns
    for col_stats in report.stats.columns:
        if col_stats.name in flagged:
            continue
        cat = col_stats.categorical
        if cat is None:
            continue
        if cat.count == 0:
            continue
        ratio = cat.cardinality / cat.count
        if ratio >= _CARDINALITY_RATIO_THRESHOLD:
            conf = confidence_score(
                row_count=row_count,
                effect=ratio,
                min_effect=_CARDINALITY_RATIO_THRESHOLD,
                max_effect=1.0,
            )
            findings.append(
                Finding(
                    type=FindingType.ID_COLUMN_DETECTED,
                    severity=Severity.LOW,
                    confidence=conf,
                    column=col_stats.name,
                    title=f"Near-unique column '{col_stats.name}' — possible ID",
                    description=(
                        f"'{col_stats.name}' has {cat.cardinality:,} unique values "
                        f"across {cat.count:,} non-null rows "
                        f"({ratio:.0%} uniqueness). "
                        f"This near-unique cardinality pattern is typical of "
                        f"identifier or free-text columns."
                    ),
                    evidence={
                        "cardinality": cat.cardinality,
                        "non_null_count": cat.count,
                        "uniqueness_ratio": round(ratio, 4),
                        "detection_method": "cardinality_ratio",
                    },
                    semantic_context=semantic_by_col.get(col_stats.name, {}).semantic_type
                    if col_stats.name in semantic_by_col
                    else None,
                )
            )

    return findings
