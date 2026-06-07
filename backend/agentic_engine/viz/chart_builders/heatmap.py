"""HEATMAP chart builder.

Produces a single correlation heatmap when the dataset has ≥ 3 numeric
columns. The matrix values come from ProfileReport.relationships.correlations
(already computed by Layer 2), so no raw data access is required.

Missing pairs (fewer than _MIN_PAIRS non-null rows for SciPy) are left as
None and the UI renders them as grey cells.
"""
from __future__ import annotations

from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.profiler.report import ProfileReport

_BASE_PRIORITY = 40
_MIN_NUMERIC_COLS = 2
_MAX_HEATMAP_COLS = 20  # matches profiler's _MAX_NUMERIC_COLS cap


def build(report: ProfileReport, idx: FindingsIndex) -> list[VizChart]:
    numeric_cols = [
        s.name
        for s in report.stats.columns
        if s.inferred_kind == "numeric" and not idx.skip(s.name)
    ]

    if len(numeric_cols) < _MIN_NUMERIC_COLS:
        return []

    cols = numeric_cols[:_MAX_HEATMAP_COLS]

    # Build symmetric matrix from the already-computed correlations.
    # Diagonal = 1.0; missing pairs = None (profiler skips pairs with < 5 rows).
    matrix: dict[str, dict[str, float | None]] = {
        c: {d: (1.0 if c == d else None) for d in cols}
        for c in cols
    }
    for corr in report.relationships.correlations:
        if corr.col_a in matrix and corr.col_b in matrix:
            r = corr.pearson  # prefer Pearson for heatmap
            matrix[corr.col_a][corr.col_b] = r
            matrix[corr.col_b][corr.col_a] = r

    # Collect finding_ids for any STRONG_CORRELATION findings touching these cols
    from agentic_engine.findings.finding_types import FindingType
    corr_findings = idx.by_type(FindingType.STRONG_CORRELATION)
    finding_ids = [
        str(f.finding_id)
        for f in corr_findings
        if f.evidence.get("col_a") in cols or f.evidence.get("col_b") in cols
    ]

    return [
        VizChart(
            chart_id="heatmap_correlations",
            type=ChartType.HEATMAP,
            title=f"Correlation Matrix ({len(cols)} numeric columns)",
            columns=cols,
            config={
                "columns": cols,
                "method": "pearson",
                "matrix": matrix,
                "color_scale": "RdBu",
                "symmetric": True,
                "show_values": len(cols) <= 10,
            },
            priority=_BASE_PRIORITY,
            finding_ids=finding_ids,
            semantic_context=None,
        )
    ]
