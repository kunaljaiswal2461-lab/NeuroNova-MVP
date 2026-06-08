"""BOXPLOT chart builder.

Generated for numeric columns where:
- outlier_pct > 0 (IQR-based outliers exist), OR
- column has a HIGH_OUTLIER_DENSITY finding from Layer 3

A boxplot complements the histogram by making the quartile structure and
outlier spread immediately visible without binning artefacts.
"""
from __future__ import annotations

from app.core.logging import get_logger
from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.findings.finding_types import FindingType
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("viz.boxplot")

_BASE_PRIORITY = 25


def _human_label(col_name: str, semantic_context: str | None) -> str:
    label = col_name.replace("_", " ").title()
    if semantic_context and semantic_context not in ("NUMERIC", "UNKNOWN"):
        label = f"{label} ({semantic_context.title()})"
    return label


def build(report: ProfileReport, idx: FindingsIndex) -> list[VizChart]:
    charts: list[VizChart] = []
    quality_by_col = {q.name: q for q in report.quality.columns}
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    for col_stats in report.stats.columns:
      try:
        if col_stats.inferred_kind != "numeric":
            continue
        if idx.skip(col_stats.name):
            continue

        num = col_stats.numeric
        if num is None or num.count == 0:
            continue

        qual = quality_by_col.get(col_stats.name)
        if qual and qual.null_pct >= 80.0:
            continue

        outlier_pct = qual.outlier_pct if qual else None
        has_outlier_finding = any(
            f.type == FindingType.HIGH_OUTLIER_DENSITY
            for f in idx.for_column(col_stats.name)
        )

        # Only generate boxplots when there's something interesting to show
        if not has_outlier_finding and (outlier_pct is None or outlier_pct == 0.0):
            # Still generate for financial/important semantics
            sem = semantic_by_col.get(col_stats.name)
            if sem not in ("FINANCIAL", "GEOGRAPHIC"):
                continue

        sem = semantic_by_col.get(col_stats.name)
        finding_ids, boost = idx.finding_boost(col_stats.name)
        priority = _BASE_PRIORITY + boost

        y_label = _human_label(col_stats.name, sem)
        charts.append(
            VizChart(
                chart_id=f"boxplot_{col_stats.name}",
                type=ChartType.BOXPLOT,
                title=f"Spread & Outliers: {y_label}",
                columns=[col_stats.name],
                config={
                    "y_col": col_stats.name,
                    "y_label": y_label,
                    "stats": {
                        "min": num.min,
                        "p25": num.p25,
                        "median": num.median,
                        "p75": num.p75,
                        "max": num.max,
                        "mean": num.mean,
                    },
                    "outlier_pct": outlier_pct,
                    "iqr": (
                        round(num.p75 - num.p25, 4)
                        if num.p75 is not None and num.p25 is not None
                        else None
                    ),
                    "null_pct": qual.null_pct if qual else 0.0,
                },
                priority=priority,
                finding_ids=finding_ids,
                semantic_context=sem,
            )
        )
      except Exception:
        logger.exception("viz.boxplot.skip", column=col_stats.name)
        continue

    return charts
