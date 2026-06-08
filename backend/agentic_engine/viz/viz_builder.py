"""VizReport orchestrator — Layer 4 entry point.

Accepts a ProfileReport (Layer 2) and a FindingsReport (Layer 3) and runs
all chart builders to produce a VizReport.

Ordering contract:
  1. timeseries  — temporal trends, highest visual impact
  2. scatter     — correlation pairs directly from L3 findings
  3. histogram   — distribution shape per numeric column
  4. boxplot     — spread/outliers for flagged or financial columns
  5. bar / pie   — categorical distributions
  6. heatmap     — correlation matrix overview (lowest priority, widest scope)

Within each builder, chart priority is further adjusted by L3 finding severity,
so a histogram for a HIGH-severity skewed column sorts above one without findings.

Skip semantics:
  Column-level builders (HISTOGRAM, BAR/PIE) silently filter columns that
  don't match their kind/cardinality/null thresholds. After all builders run,
  the orchestrator diffs intended-chartable columns against actually-charted
  columns and emits a ``SkippedColumn`` entry for each gap so the UI can
  surface a one-line reason instead of leaving the user guessing.

  Dataset-level charts (HEATMAP, SCATTER, TIMESERIES) are NOT enrolled in
  the skip diff — they operate over the whole profile or finding-pairs and
  can't be attributed to a single column.
"""
from __future__ import annotations

from app.core.logging import get_logger
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding, FindingsReport
from agentic_engine.profiler.report import ColumnStats, ProfileReport
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.viz.chart_builders import (
    bar_charts,
    boxplot,
    health_radar,
    heatmap,
    histograms,
    null_matrix,
    scatter,
    timeseries,
)
from agentic_engine.viz.models import ChartType, SkippedColumn, VizReport


logger = get_logger("viz.builder")

_BUILDERS = [
    timeseries,
    scatter,
    histograms,
    boxplot,
    bar_charts,
    heatmap,
    null_matrix,
    health_radar,
]

# Dataset-level chart types — not attributable to a single column, so they
# never contribute to "rendered_columns" for the skip diff.
_DATASET_LEVEL_TYPES = {
    ChartType.HEATMAP,
    ChartType.SCATTER,
    ChartType.TIMESERIES,
    ChartType.NULL_MATRIX,
    ChartType.RADAR,
}

# Severity ranking for picking the "most relevant" finding per column.
_SEVERITY_RANK = {Severity.HIGH: 3, Severity.MEDIUM: 2, Severity.LOW: 1}

_NULL_PCT_HEAVY = 95.0
_NULL_PCT_HARD_SKIP = 80.0  # mirrors histogram/bar builder threshold


def _intended_chart_type(col: ColumnStats) -> str:
    """Map a column's inferred_kind to the chart type the user would expect.

    Returns "NONE" for kinds that don't have a column-level chart
    (datetime is consumed by timeseries; text/other have no canonical chart).
    """
    if col.inferred_kind == "numeric":
        return ChartType.HISTOGRAM.value
    if col.inferred_kind in ("categorical", "boolean"):
        return ChartType.BAR.value
    if col.inferred_kind == "datetime":
        return "NONE"  # consumed by dataset-level timeseries
    return "NONE"


def _should_enroll_for_skip(col: ColumnStats) -> bool:
    """True if this column should appear in skipped_columns when no chart
    is produced for it.

    Datetime columns are consumed by the dataset-level timeseries builder
    and are not eligible for the per-column skip diff. Every other inferred
    kind is enrolled — including ``text`` and ``other``, which is how we
    surface the common case of dirty numeric columns (e.g. ``"$183.42"``)
    that the profiler classified as text.
    """
    return col.inferred_kind != "datetime"


def _top_finding_for_column(column: str, findings: FindingsReport) -> Finding | None:
    """Return the finding with the highest severity referencing this column.

    Ties broken by confidence. Returns None if the column has no findings.
    """
    relevant = [f for f in findings.findings if f.column == column]
    if not relevant:
        return None
    relevant.sort(
        key=lambda f: (_SEVERITY_RANK.get(f.severity, 0), f.confidence),
        reverse=True,
    )
    return relevant[0]


def _infer_skip_reason(
    col: ColumnStats,
    null_pct: float | None,
    semantic_type: str | None,
    is_constant: bool,
    idx: FindingsIndex,
) -> str:
    """Return a plain-English reason this column was not charted.

    Inspects column quality, semantic, and the findings index to produce
    a one-sentence explanation. Order is intentional: most-specific first.
    """
    if is_constant:
        return "Column is constant — no variation to visualize."
    if idx.skip(col.name) and semantic_type == "IDENTIFIER":
        return "Column is an identifier and not suitable for distribution charts."
    if null_pct is not None:
        if null_pct >= 100.0:
            return "Column is entirely null."
        if null_pct >= _NULL_PCT_HEAVY:
            return f"Column is {null_pct:.0f}%+ null — insufficient data for a chart."
        if null_pct >= _NULL_PCT_HARD_SKIP:
            return f"Column is {null_pct:.0f}% null — above the {int(_NULL_PCT_HARD_SKIP)}% threshold for charting."
    if col.inferred_kind == "numeric":
        num = col.numeric
        if num is None or num.count == 0:
            return "Numeric column has no usable values."
        return "Numeric column could not be parsed for visualization."
    if col.inferred_kind in ("categorical", "boolean"):
        cat = col.categorical
        if cat is None or cat.count == 0:
            return "Categorical column has no usable values."
        return "Categorical column could not be charted."
    if col.inferred_kind == "text":
        return "Column contains free-form text that does not fit a chart pattern."
    if col.inferred_kind == "other":
        return "Column type could not be classified for charting."
    return "Column could not be parsed for visualization."


def build_viz(
    profile: ProfileReport,
    findings: FindingsReport,
) -> VizReport:
    """Run all chart builders and return a priority-sorted VizReport.

    Also computes ``skipped_columns`` so the UI can show *why* an expected
    column is missing instead of leaving the user guessing.
    """
    idx = FindingsIndex(findings)
    all_charts = []

    for builder in _BUILDERS:
        try:
            charts = builder.build(profile, idx)
            all_charts.extend(charts)
            logger.debug(
                "viz.builder_done",
                builder=builder.__name__.split(".")[-1],
                charts=len(charts),
            )
        except Exception:
            logger.exception("viz.builder_error", builder=builder.__name__)

    # Deduplicate by chart_id (safety net — each builder uses unique id prefixes)
    seen: set[str] = set()
    deduped = []
    for chart in all_charts:
        if chart.chart_id not in seen:
            seen.add(chart.chart_id)
            deduped.append(chart)

    deduped.sort(key=lambda c: c.priority)

    # ── Build skipped_columns ────────────────────────────────────────────────
    # A column is "charted" if at least one column-level chart references it.
    charted_cols: set[str] = set()
    for chart in deduped:
        if chart.type in _DATASET_LEVEL_TYPES:
            continue
        for col_name in chart.columns:
            charted_cols.add(col_name)

    quality_by_col = {q.name: q for q in profile.quality.columns}
    semantic_by_col = {s.name: s.semantic_type for s in profile.semantic.columns}

    skipped: list[SkippedColumn] = []
    for col_stats in profile.stats.columns:
        if not _should_enroll_for_skip(col_stats):
            continue
        if col_stats.name in charted_cols:
            continue
        intended = _intended_chart_type(col_stats)

        qual = quality_by_col.get(col_stats.name)
        null_pct = qual.null_pct if qual else None
        is_constant = bool(qual and qual.is_constant)
        semantic_type = semantic_by_col.get(col_stats.name)

        top_finding = _top_finding_for_column(col_stats.name, findings)
        finding_type_str = top_finding.type.value if top_finding else ""

        reason = _infer_skip_reason(
            col=col_stats,
            null_pct=null_pct,
            semantic_type=semantic_type,
            is_constant=is_constant,
            idx=idx,
        )

        skipped.append(
            SkippedColumn(
                column_name=col_stats.name,
                intended_chart_type=intended,
                reason=reason,
                finding_type=finding_type_str,
                null_pct=null_pct,
            )
        )

    total_columns = profile.schema_.col_count
    # rendered_columns counts distinct columns that produced a column-level chart
    rendered_columns = len(charted_cols)

    logger.info(
        "viz.built",
        dataset_id=str(profile.dataset_id),
        total_charts=len(deduped),
        skipped_cols=len(skipped),
        rendered_columns=rendered_columns,
        total_columns=total_columns,
    )

    return VizReport(
        dataset_id=profile.dataset_id,
        charts=deduped,
        skipped_columns=skipped,
        total_columns=total_columns,
        rendered_columns=rendered_columns,
    )
