"""SCATTER chart builder.

One scatter plot per STRONG_CORRELATION finding from Layer 3.
This is the tightest Layer 3 → Layer 4 coupling: the finding carries
col_a, col_b, pearson, and spearman directly in its evidence dict,
so no re-computation is needed here — except for the raw sample points,
which come from the matching Correlation entry in relationships.correlations.
"""
from __future__ import annotations

from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.findings.finding_types import FindingType
from agentic_engine.profiler.report import ProfileReport

_BASE_PRIORITY = 15


def _human_label(col_name: str, semantic_by_col: dict) -> str:
    sem = semantic_by_col.get(col_name)
    label = col_name.replace("_", " ").title()
    if sem and sem not in ("NUMERIC", "UNKNOWN"):
        label = f"{label} ({sem.title()})"
    return label


def build(report: ProfileReport, idx: FindingsIndex) -> list[VizChart]:
    charts: list[VizChart] = []
    semantic_by_col = {s.name: s.semantic_type for s in report.semantic.columns}
    seen_pairs: set[frozenset[str]] = set()

    # Build a lookup so we can find the raw sample points for any (col_a, col_b) pair,
    # regardless of which order the finding references them in.
    corr_by_pair = {
        frozenset({c.col_a, c.col_b}): c for c in report.relationships.correlations
    }

    corr_findings = idx.by_type(FindingType.STRONG_CORRELATION)

    for finding in corr_findings:
        ev = finding.evidence
        col_a = ev.get("col_a")
        col_b = ev.get("col_b")
        if not col_a or not col_b:
            continue

        pair = frozenset({col_a, col_b})
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        if idx.skip(col_a) or idx.skip(col_b):
            continue

        pearson = ev.get("pearson")
        spearman = ev.get("spearman")
        best_r = ev.get("best_r", pearson or spearman or 0)
        abs_r = abs(best_r)

        priority = _BASE_PRIORITY - int(abs_r * 10)

        x_label = _human_label(col_a, semantic_by_col)
        y_label = _human_label(col_b, semantic_by_col)
        direction = "positive" if best_r > 0 else "negative"

        corr_entry = corr_by_pair.get(pair)
        sample_x = corr_entry.sample_x if corr_entry else None
        sample_y = corr_entry.sample_y if corr_entry else None
        # corr_by_pair's key order may not match col_a/col_b order — swap if needed
        if corr_entry and corr_entry.col_a != col_a:
            sample_x, sample_y = sample_y, sample_x

        charts.append(
            VizChart(
                chart_id=f"scatter_{col_a}_vs_{col_b}",
                type=ChartType.SCATTER,
                title=f"{x_label} vs {y_label} (r={best_r:.2f})",
                columns=[col_a, col_b],
                config={
                    "x_col": col_a,
                    "y_col": col_b,
                    "x_label": x_label,
                    "y_label": y_label,
                    "pearson_r": pearson,
                    "spearman_r": spearman,
                    "best_r": best_r,
                    "direction": direction,
                    "show_trendline": True,
                    "sample_x": sample_x,   # NEW
                    "sample_y": sample_y,   # NEW
                    "correlation_strength": (
                        "very_strong" if abs_r >= 0.95
                        else "strong" if abs_r >= 0.85
                        else "moderate"
                    ),
                },
                priority=priority,
                finding_ids=[str(finding.finding_id)],
                semantic_context=None,
            )
        )

    return charts