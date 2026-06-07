"""BAR and PIE chart builders for categorical columns.

BAR  — categorical/boolean columns with cardinality 2–30.
       Top 15 values, plus an "Other" bucket aggregating the remaining
       (cardinality − 15) values when there are more than 15 unique values.

PIE  — categorical/boolean columns with cardinality 2–30.
       Top 8 values, plus an "Other" bucket aggregating the remaining
       (cardinality − 8) values when there are more than 8 unique values.

Priority boosted when an IMBALANCED_DISTRIBUTION finding from Layer 3 references
the same column — imbalanced distributions are visually important.

Top-N values come directly from CategoricalStats.top_values (pre-computed by
the profiler), so no raw data access is needed here.
"""
from __future__ import annotations

from app.core.logging import get_logger
from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.findings.finding_types import FindingType
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("viz.bar_charts")

_BAR_BASE_PRIORITY = 30
_PIE_BASE_PRIORITY = 35
_BAR_TOP_N = 15
_PIE_TOP_N = 8


def _with_other_bucket(
    top_values: list, top_n: int, total_count: int, cardinality: int = 0
) -> list:
    """Take the first top_n items from ``top_values`` and append an
    ('Other', remaining_count) tuple if any values are dropped.

    ``top_values`` is the ``CategoricalStats.top_values`` list (sorted by
    frequency descending). ``cardinality`` is the column's true unique-value
    count — needed because the profiler may store only the top-N values even
    when cardinality is much higher, making ``len(top_values)`` an unreliable
    indicator of whether an "Other" bucket is required.
    """
    head = top_values[:top_n]
    # Use the larger of the stored list length and the true cardinality so we
    # add an "Other" bucket whenever there are values beyond what we're showing.
    true_unique = max(len(top_values), cardinality)
    if true_unique <= len(head):
        return head
    shown_count = sum(int(c) for _, c in head)
    other_count = max(total_count - shown_count, 0)
    if other_count <= 0:
        return head
    return head + [("Other", other_count)]


def _human_label(col_name: str, semantic_context: str | None) -> str:
    label = col_name.replace("_", " ").title()
    if semantic_context and semantic_context not in ("CATEGORICAL", "BOOLEAN", "UNKNOWN"):
        label = f"{label} ({semantic_context.title()})"
    return label


def build(report: ProfileReport, idx: FindingsIndex) -> list[VizChart]:
    charts: list[VizChart] = []
    quality_by_col = {q.name: q for q in report.quality.columns}
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}

    for col_stats in report.stats.columns:
      try:
        if col_stats.inferred_kind not in ("categorical", "boolean"):
            continue
        if idx.skip(col_stats.name):
            continue

        cat = col_stats.categorical
        if cat is None or cat.count == 0:
            continue

        qual = quality_by_col.get(col_stats.name)
        if qual and qual.null_pct >= 80.0:
            continue

        sem = semantic_by_col.get(col_stats.name)
        finding_ids, boost = idx.finding_boost(col_stats.name)
        label = _human_label(col_stats.name, sem)

        bar_values = _with_other_bucket(cat.top_values, _BAR_TOP_N, cat.count, cat.cardinality)
        pie_values = _with_other_bucket(cat.top_values, _PIE_TOP_N, cat.count, cat.cardinality)

        # BAR chart
        charts.append(
            VizChart(
                chart_id=f"bar_{col_stats.name}",
                type=ChartType.BAR,
                title=f"Value Distribution: {label}",
                columns=[col_stats.name],
                config={
                    "x_col": col_stats.name,
                    "y_col": "count",
                    "x_label": label,
                    "y_label": "Count",
                    "top_n": _BAR_TOP_N,
                    "top_values": bar_values,
                    "cardinality": cat.cardinality,
                    "mode": cat.mode,
                    "null_pct": qual.null_pct if qual else 0.0,
                    "show_pct_labels": True,
                    "total_count": cat.count,
                    "has_other_bucket": cat.cardinality > _BAR_TOP_N,
                },
                priority=_BAR_BASE_PRIORITY + boost,
                finding_ids=finding_ids,
                semantic_context=sem,
            )
        )

        # PIE chart — generated for any categorical with ≥2 distinct values.
        # The Other-bucket logic keeps the chart readable even at high
        # cardinality.
        if len(pie_values) >= 2:
            charts.append(
                VizChart(
                    chart_id=f"pie_{col_stats.name}",
                    type=ChartType.PIE,
                    title=f"Proportion: {label}",
                    columns=[col_stats.name],
                    config={
                        "col": col_stats.name,
                        "values": pie_values,
                        "top_n": _PIE_TOP_N,
                        "cardinality": cat.cardinality,
                        "label_col": col_stats.name,
                        "total_count": cat.count,
                        "null_pct": qual.null_pct if qual else 0.0,
                        "has_other_bucket": cat.cardinality > _PIE_TOP_N,
                    },
                    priority=_PIE_BASE_PRIORITY + boost,
                    finding_ids=finding_ids,
                    semantic_context=sem,
                )
            )
      except Exception:
        logger.exception("viz.bar.skip", column=col_stats.name)
        continue

    return charts
