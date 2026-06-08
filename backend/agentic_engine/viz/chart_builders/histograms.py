"""HISTOGRAM chart builder.

One histogram per numeric column that:
- is not constant (zero variance)
- is not a high-confidence identifier
- has null_pct < 80%

Priority boosted if the column has HIGH/MEDIUM severity findings from Layer 3.
Config includes pre-computed stats so the UI doesn't need to re-derive them.
"""
from __future__ import annotations

from app.core.logging import get_logger
from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.findings.finding_types import FindingType
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("viz.histograms")

_BASE_PRIORITY = 20
_MAX_BINS = 50
_MIN_BINS = 10
_BIN_FACTOR = 0.05  # bins ≈ 5% of non-null count, clamped


def _bin_count(non_null_count: int) -> int:
    bins = max(_MIN_BINS, min(_MAX_BINS, int(non_null_count * _BIN_FACTOR)))
    return bins


def _human_label(col_name: str, semantic_context: str | None) -> str:
    label = col_name.replace("_", " ").title()
    if semantic_context and semantic_context not in ("NUMERIC", "UNKNOWN", "TEXT"):
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

            sem = semantic_by_col.get(col_stats.name)
            finding_ids, boost = idx.finding_boost(col_stats.name)
            priority = _BASE_PRIORITY + boost

            # Detect skew note from findings
            skew_findings = [
                f for f in idx.for_column(col_stats.name)
                if f.type == FindingType.SKEWED_DISTRIBUTION
            ]
            is_skewed = len(skew_findings) > 0

            x_label = _human_label(col_stats.name, sem)
            charts.append(
                VizChart(
                    chart_id=f"histogram_{col_stats.name}",
                    type=ChartType.HISTOGRAM,
                    title=f"Distribution of {x_label}",
                    columns=[col_stats.name],
                    config={
                        "x_col": col_stats.name,
                        "bins": _bin_count(num.count),
                        "x_label": x_label,
                        "y_label": "Count",
                        "stats": {
                            "mean": num.mean,
                            "median": num.median,
                            "std": num.std,
                            "min": num.min,
                            "max": num.max,
                            "p25": num.p25,
                            "p75": num.p75,
                            "skew": num.skew,
                        },
                        "is_skewed": is_skewed,
                        "skew_value": num.skew,
                        "show_mean_line": True,
                        "show_median_line": True,
                        "null_pct": qual.null_pct if qual else 0.0,
                    },
                    priority=priority,
                    finding_ids=finding_ids,
                    semantic_context=sem,
                )
            )
        except Exception:
            logger.exception("viz.histogram.skip", column=col_stats.name)
            continue

    return charts
