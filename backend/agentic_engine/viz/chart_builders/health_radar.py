"""RADAR chart builder for dataset-level health score.

Reads ProfileReport.health directly — component scores are already
computed by Layer 2's health_scorer. Cannot fail: if ProfileReport
exists, this works.
"""
from __future__ import annotations

from app.core.logging import get_logger
from agentic_engine.viz.models import ChartType, VizChart
from agentic_engine.viz._findings_index import FindingsIndex
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("viz.health_radar")

_BASE_PRIORITY = 50  # lowest priority — most general overview


def build(report: ProfileReport, idx: FindingsIndex) -> list[VizChart]:
    health = report.health
    if not health or not health.components:
        return []

    dimensions = list(health.components.keys())
    scores = [float(v) for v in health.components.values()]

    return [
        VizChart(
            chart_id="health_radar",
            type=ChartType.RADAR,
            title=f"Dataset Health Radar (score {health.score:.0f}, grade {health.grade})",
            columns=[],  # dataset-level, no specific columns
            config={
                "dimensions": dimensions,
                "scores": scores,
                "overall_score": float(health.score),
                "grade": health.grade,
                "axis_max": 100.0,
            },
            priority=_BASE_PRIORITY,
            finding_ids=[],
            semantic_context=None,
        )
    ]
