"""TIMESERIES chart builder.

Generates one time-series chart when the dataset has at least one TEMPORAL
column (datetime kind from the profiler) paired with at least one numeric
column. The x-axis is always the datetime column; y-axes are numeric columns
filtered by Layer 3 (constants and identifiers are skipped).

Aggregation granularity is inferred from range_days so the UI can bucket
rows before plotting without needing raw data.
"""
from __future__ import annotations

from app.core.logging import get_logger
from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("viz.timeseries")

_BASE_PRIORITY = 10  # timeseries is visually compelling — high priority
_MAX_Y_COLS = 5


def _infer_aggregate(range_days: float | None) -> str:
    if range_days is None:
        return "none"
    if range_days > 730:
        return "monthly"
    if range_days > 90:
        return "weekly"
    if range_days > 14:
        return "daily"
    return "none"


def build(report: ProfileReport, idx: FindingsIndex) -> list[VizChart]:
    charts: list[VizChart] = []
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    datetime_cols = [
        s for s in report.stats.columns
        if s.inferred_kind == "datetime" and not idx.skip(s.name)
    ]
    numeric_cols = [
        s for s in report.stats.columns
        if s.inferred_kind == "numeric"
        and not idx.skip(s.name)
        and s.numeric is not None
        and s.numeric.count > 0
    ]

    if not datetime_cols or not numeric_cols:
        return []

    for dt_col in datetime_cols:
      try:
        dt_stats = dt_col.datetime_
        if dt_stats is None:
            continue

        # Prefer financial and numeric columns for y-axes; skip identifiers already
        y_cols_stats = numeric_cols[:_MAX_Y_COLS]
        if not y_cols_stats:
            continue

        y_col_names = [s.name for s in y_cols_stats]
        y_labels = [
            s.name.replace("_", " ").title() + (
                f" ({semantic_by_col[s.name].title()})"
                if semantic_by_col.get(s.name) not in (None, "NUMERIC", "UNKNOWN")
                else ""
            )
            for s in y_cols_stats
        ]

        aggregate = _infer_aggregate(dt_stats.range_days)
        x_label = dt_col.name.replace("_", " ").title()

        # Collect any finding_ids related to the y columns
        finding_ids: list[str] = []
        for col_name in y_col_names:
            for f in idx.for_column(col_name):
                fid = str(f.finding_id)
                if fid not in finding_ids:
                    finding_ids.append(fid)

        charts.append(
            VizChart(
                chart_id=f"timeseries_{dt_col.name}",
                type=ChartType.TIMESERIES,
                title=f"Trends over {x_label}",
                columns=[dt_col.name] + y_col_names,
                config={
                    "x_col": dt_col.name,
                    "y_cols": y_col_names,
                    "x_label": x_label,
                    "y_labels": y_labels,
                    "aggregate": aggregate,
                    "range_days": dt_stats.range_days,
                    "x_min": dt_stats.min.isoformat() if dt_stats.min else None,
                    "x_max": dt_stats.max.isoformat() if dt_stats.max else None,
                    "row_count": dt_stats.count,
                },
                priority=_BASE_PRIORITY,
                finding_ids=finding_ids,
                semantic_context="TEMPORAL",
            )
        )
      except Exception:
        logger.exception("viz.timeseries.skip", column=dt_col.name)
        continue

    return charts
