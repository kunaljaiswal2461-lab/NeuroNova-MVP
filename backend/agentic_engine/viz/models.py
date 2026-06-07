"""VizReport data models — the output of Layer 4.

A VizReport describes *which* charts to render and *how* to configure them,
without embedding any raw data. The Streamlit UI reads a VizReport and draws
charts by fetching column values from the ProfileReport stats section.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ChartType(str, enum.Enum):
    HISTOGRAM = "HISTOGRAM"
    BOXPLOT = "BOXPLOT"
    BAR = "BAR"
    PIE = "PIE"
    SCATTER = "SCATTER"
    HEATMAP = "HEATMAP"
    TIMESERIES = "TIMESERIES"
    NULL_MATRIX = "NULL_MATRIX"
    RADAR = "RADAR"


class VizChart(BaseModel):
    """Specification for a single chart.

    `config` is chart-type-specific and contains axis labels, pre-computed
    stats, and rendering hints. It never contains raw row-level data.
    `finding_ids` links this chart back to the Layer 3 findings that motivated
    its generation — used for traceability in the UI and LLM grounding.
    """

    chart_id: str
    type: ChartType
    title: str
    columns: list[str]
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 50
    finding_ids: list[str] = Field(default_factory=list)
    semantic_context: str | None = None


class SkippedColumn(BaseModel):
    """A column that the viz layer chose not to render a chart for.

    Persisted alongside the chart list so the UI can surface a one-line
    reason per skipped column instead of leaving the user wondering why
    a column they expected to see is missing.
    """

    column_name: str
    intended_chart_type: str  # ChartType value (HISTOGRAM/BAR/TIMESERIES) or "NONE"
    reason: str               # plain English, one sentence
    finding_type: str = ""    # most relevant FindingType for the column, or empty
    null_pct: float | None = None


class VizReport(BaseModel):
    """Top-level output of the visualization metadata layer.

    Persisted as JSON to data/viz/{dataset_id}.json and read by the
    Visualization Center page and the LLM Insight Layer (Layer 5).
    """

    dataset_id: uuid.UUID
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    charts: list[VizChart] = Field(default_factory=list)
    skipped_columns: list[SkippedColumn] = Field(default_factory=list)
    total_columns: int = 0
    rendered_columns: int = 0

    @property
    def count(self) -> int:
        return len(self.charts)

    def by_type(self, chart_type: ChartType) -> list[VizChart]:
        return [c for c in self.charts if c.type == chart_type]

    def for_columns(self, *cols: str) -> list[VizChart]:
        col_set = set(cols)
        return [c for c in self.charts if col_set.intersection(c.columns)]

    def sorted_by_priority(self) -> list[VizChart]:
        return sorted(self.charts, key=lambda c: c.priority)
