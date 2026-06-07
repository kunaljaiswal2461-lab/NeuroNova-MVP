"""NULL_MATRIX chart builder.

One dataset-level chart showing the null percentage for every column.

This is the per-column variant of the classic missingno matrix — the
viz layer does not have raw data access (only ProfileReport), so a
row-level matrix would require plumbing the DataFrame through the
pipeline. The per-column view answers the same "what's missing where?"
question without that change.

Always produced when the dataset has at least one column. Cannot fail
in any meaningful way: it reads pre-computed null_pct values.
"""
from __future__ import annotations

from app.core.logging import get_logger
from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("viz.null_matrix")

_BASE_PRIORITY = 45  # below heatmap, above nothing


def build(report: ProfileReport, idx: FindingsIndex) -> list[VizChart]:
    quality_cols = report.quality.columns
    if not quality_cols:
        return []

    columns = [q.name for q in quality_cols]
    null_pcts = [float(q.null_pct) for q in quality_cols]

    # Highlight columns with non-trivial null rates
    flagged = [c for c, p in zip(columns, null_pcts) if p >= 5.0]

    return [
        VizChart(
            chart_id="null_matrix",
            type=ChartType.NULL_MATRIX,
            title=f"Null Distribution — {len(columns)} columns",
            columns=columns,
            config={
                "columns": columns,
                "null_pcts": null_pcts,
                "flagged_columns": flagged,
                "x_label": "Column",
                "y_label": "Null %",
                "row_count": report.schema_.row_count,
            },
            priority=_BASE_PRIORITY,
            finding_ids=[],
            semantic_context=None,
        )
    ]
