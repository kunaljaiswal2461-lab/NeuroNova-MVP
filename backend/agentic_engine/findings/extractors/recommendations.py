"""DATA_RECOMMENDATION extractor.

Derives actionable recommendations by combining signals from multiple
findings already in the report context. This runs last in the pipeline
and consumes the accumulated findings list, not the ProfileReport directly.

Each recommendation is a concrete, one-step action a data practitioner
can take — not a vague observation.
"""
from __future__ import annotations

from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding
from agentic_engine.profiler.report import ProfileReport

# Threshold above which a null rate becomes "imputation-worthy"
_IMPUTE_NULL_PCT = 20.0
# Threshold above which a null rate suggests outright removal
_DROP_NULL_PCT = 70.0
# Skew threshold that warrants a log-transform recommendation
_LOG_TRANSFORM_SKEW = 1.5
# Outlier density that warrants a cap/floor recommendation
_CAP_OUTLIER_PCT = 10.0


def extract_from_findings(
    report: ProfileReport,
    prior_findings: list[Finding],
) -> list[Finding]:
    """Called by findings_builder after all other extractors have run."""
    recommendations: list[Finding] = []

    null_by_col: dict[str, float] = {}
    outlier_by_col: dict[str, float] = {}
    skew_by_col: dict[str, float] = {}
    constant_cols: set[str] = set()
    id_cols: set[str] = set()
    has_duplicates = False
    correlation_pairs: list[tuple[str, str, float]] = []

    for f in prior_findings:
        if f.type == FindingType.HIGH_NULLABILITY and f.column:
            null_by_col[f.column] = f.evidence.get("null_pct", 0)
        elif f.type == FindingType.HIGH_OUTLIER_DENSITY and f.column:
            outlier_by_col[f.column] = f.evidence.get("outlier_pct", 0)
        elif f.type == FindingType.SKEWED_DISTRIBUTION and f.column:
            skew_by_col[f.column] = abs(f.evidence.get("skew", 0))
        elif f.type == FindingType.CONSTANT_COLUMN and f.column:
            constant_cols.add(f.column)
        elif f.type == FindingType.ID_COLUMN_DETECTED and f.column:
            id_cols.add(f.column)
        elif f.type == FindingType.DUPLICATE_ROWS:
            has_duplicates = True
        elif f.type == FindingType.STRONG_CORRELATION:
            ev = f.evidence
            if ev.get("col_a") and ev.get("col_b"):
                correlation_pairs.append(
                    (ev["col_a"], ev["col_b"], abs(ev.get("best_r", 0)))
                )

    # Drop constant columns
    if constant_cols:
        cols_str = ", ".join(f"'{c}'" for c in sorted(constant_cols))
        recommendations.append(
            Finding(
                type=FindingType.DATA_RECOMMENDATION,
                severity=Severity.HIGH,
                confidence=0.95,
                column=None,
                title=f"Drop {len(constant_cols)} constant column(s)",
                description=(
                    f"Remove {cols_str} — they contain a single value across all rows "
                    f"and add no information for analysis or modelling."
                ),
                evidence={
                    "action": "drop_columns",
                    "columns": sorted(constant_cols),
                    "reason": "zero_variance",
                },
            )
        )

    # Impute or drop high-null columns
    drop_cols = [c for c, p in null_by_col.items() if p >= _DROP_NULL_PCT]
    impute_cols = [c for c, p in null_by_col.items()
                   if _IMPUTE_NULL_PCT <= p < _DROP_NULL_PCT]

    if drop_cols:
        recommendations.append(
            Finding(
                type=FindingType.DATA_RECOMMENDATION,
                severity=Severity.HIGH,
                confidence=0.88,
                column=None,
                title=f"Consider dropping {len(drop_cols)} column(s) with >70% nulls",
                description=(
                    f"Columns {[f'{c} ({null_by_col[c]:.0f}%)' for c in drop_cols]} "
                    f"have extreme missingness. Imputation at this rate introduces "
                    f"more noise than signal."
                ),
                evidence={
                    "action": "drop_or_investigate_columns",
                    "columns": drop_cols,
                    "null_pcts": {c: null_by_col[c] for c in drop_cols},
                },
            )
        )

    if impute_cols:
        recommendations.append(
            Finding(
                type=FindingType.DATA_RECOMMENDATION,
                severity=Severity.MEDIUM,
                confidence=0.80,
                column=None,
                title=f"Impute {len(impute_cols)} column(s) with moderate missingness",
                description=(
                    f"Columns {impute_cols} have 20–70% missing values. "
                    f"Consider median/mode imputation for numeric/categorical columns, "
                    f"or model-based imputation if missingness is not at random."
                ),
                evidence={
                    "action": "impute_columns",
                    "columns": impute_cols,
                    "null_pcts": {c: null_by_col[c] for c in impute_cols},
                },
            )
        )

    # Log-transform skewed columns
    log_candidates = [c for c, s in skew_by_col.items() if s >= _LOG_TRANSFORM_SKEW]
    if log_candidates:
        recommendations.append(
            Finding(
                type=FindingType.DATA_RECOMMENDATION,
                severity=Severity.MEDIUM,
                confidence=0.75,
                column=None,
                title=f"Apply log/power transform to {len(log_candidates)} skewed column(s)",
                description=(
                    f"Columns {log_candidates} are significantly skewed. "
                    f"A log1p or Box-Cox transformation will reduce skew and improve "
                    f"model performance for algorithms that assume normality."
                ),
                evidence={
                    "action": "log_transform",
                    "columns": log_candidates,
                    "skews": {c: skew_by_col[c] for c in log_candidates},
                },
            )
        )

    # Cap outliers
    cap_candidates = [c for c, p in outlier_by_col.items() if p >= _CAP_OUTLIER_PCT]
    if cap_candidates:
        recommendations.append(
            Finding(
                type=FindingType.DATA_RECOMMENDATION,
                severity=Severity.MEDIUM,
                confidence=0.72,
                column=None,
                title=f"Winsorise or cap outliers in {len(cap_candidates)} column(s)",
                description=(
                    f"Columns {cap_candidates} have high outlier density (>10% IQR outliers). "
                    f"Consider winsorising at the 1st/99th percentile or applying "
                    f"robust scaling before model training."
                ),
                evidence={
                    "action": "winsorise",
                    "columns": cap_candidates,
                    "outlier_pcts": {c: outlier_by_col[c] for c in cap_candidates},
                },
            )
        )

    # Deduplicate
    if has_duplicates:
        recommendations.append(
            Finding(
                type=FindingType.DATA_RECOMMENDATION,
                severity=Severity.HIGH,
                confidence=0.92,
                column=None,
                title="Deduplicate rows before analysis",
                description=(
                    "The dataset contains exact duplicate rows. Remove them with "
                    "df.drop_duplicates() (Pandas) or df.unique() (Polars) before "
                    "computing statistics or training models to avoid inflated sample counts."
                ),
                evidence={
                    "action": "drop_duplicates",
                    "duplicate_pct": report.quality.duplicate_row_pct,
                },
            )
        )

    # Feature selection for highly correlated pairs
    if len(correlation_pairs) >= 2:
        top_pairs = sorted(correlation_pairs, key=lambda x: x[2], reverse=True)[:3]
        recommendations.append(
            Finding(
                type=FindingType.DATA_RECOMMENDATION,
                severity=Severity.LOW,
                confidence=0.70,
                column=None,
                title=f"Review {len(correlation_pairs)} highly correlated feature pair(s)",
                description=(
                    f"Found {len(correlation_pairs)} numeric pairs with |r| ≥ 0.70. "
                    f"Consider removing one column from each pair or applying PCA "
                    f"to reduce redundancy. Top pairs: "
                    + "; ".join(
                        f"'{a}'↔'{b}' (r={r:.2f})" for a, b, r in top_pairs
                    )
                ),
                evidence={
                    "action": "feature_selection_or_pca",
                    "correlated_pair_count": len(correlation_pairs),
                    "top_pairs": [
                        {"col_a": a, "col_b": b, "r": r} for a, b, r in top_pairs
                    ],
                },
            )
        )

    return recommendations


def extract(report: ProfileReport) -> list[Finding]:
    # This extractor is invoked with prior_findings by findings_builder.
    # When called standalone it returns an empty list.
    return []
