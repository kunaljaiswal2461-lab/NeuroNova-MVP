"""Pydantic data models that make up a ProfileReport.

Kept here (not in app/schemas) because they belong to the agentic_engine
domain — they are produced by the profiler and consumed by Layer 3+ (findings,
viz, LLM, retrieval). The API layer re-exports them for response payloads.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SemanticType = Literal[
    "IDENTIFIER",
    "FINANCIAL",
    "GEOGRAPHIC",
    "TEMPORAL",
    "CATEGORICAL",
    "BOOLEAN",
    "NUMERIC",
    "TEXT",
    "EMAIL",
    "URL",
    "PHONE",
    "UNKNOWN",
]


HealthGrade = Literal["A", "B", "C", "D", "F"]


class SchemaColumn(BaseModel):
    name: str
    dtype: str
    polars_dtype: str
    nullable: bool


class SchemaSection(BaseModel):
    row_count: int
    col_count: int
    columns: list[SchemaColumn]


class NumericStats(BaseModel):
    count: int
    mean: float | None = None
    std: float | None = None
    min: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    max: float | None = None
    skew: float | None = None
    kurtosis: float | None = None


class CategoricalStats(BaseModel):
    count: int
    cardinality: int
    mode: str | None = None
    top_values: list[tuple[str, int]] = Field(default_factory=list)


class DatetimeStats(BaseModel):
    count: int
    min: datetime | None = None
    max: datetime | None = None
    range_days: float | None = None


class ColumnStats(BaseModel):
    name: str
    inferred_kind: Literal["numeric", "categorical", "datetime", "boolean", "text", "other"]
    numeric: NumericStats | None = None
    categorical: CategoricalStats | None = None
    datetime_: DatetimeStats | None = Field(default=None, alias="datetime")

    model_config = ConfigDict(populate_by_name=True)


class StatsSection(BaseModel):
    columns: list[ColumnStats]


class ColumnQuality(BaseModel):
    name: str
    null_pct: float
    is_constant: bool
    outlier_pct: float | None = None  # numeric only (IQR-based)


class QualitySection(BaseModel):
    duplicate_row_pct: float
    columns: list[ColumnQuality]


class Correlation(BaseModel):
    col_a: str
    col_b: str
    pearson: float | None = None
    spearman: float | None = None


class RelationshipSection(BaseModel):
    correlations: list[Correlation]


class SemanticTag(BaseModel):
    name: str
    semantic_type: SemanticType
    confidence: float


class SemanticSection(BaseModel):
    columns: list[SemanticTag]


class HealthSection(BaseModel):
    score: float  # 0..100
    grade: HealthGrade
    components: dict[str, float]


class ProfileReport(BaseModel):
    """Top-level output of the profiling engine.

    Persisted as JSON to data/profiles/{dataset_id}.json and re-read by later
    layers (findings, viz, LLM summary, retrieval).
    """

    dataset_id: uuid.UUID
    generated_at: datetime
    schema_: SchemaSection = Field(alias="schema")
    stats: StatsSection
    quality: QualitySection
    relationships: RelationshipSection
    semantic: SemanticSection
    health: HealthSection

    model_config = ConfigDict(populate_by_name=True)

    def model_dump_json_safe(self) -> dict[str, Any]:
        """Dump using field aliases (schema, datetime) and json-mode serializers."""
        return self.model_dump(mode="json", by_alias=True)
