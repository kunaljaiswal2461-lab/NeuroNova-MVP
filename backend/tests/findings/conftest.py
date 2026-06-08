"""Shared ProfileReport fixtures for findings extractor tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from agentic_engine.profiler.report import (
    ColumnQuality,
    ColumnStats,
    Correlation,
    HealthSection,
    NumericStats,
    CategoricalStats,
    ProfileReport,
    QualitySection,
    RelationshipSection,
    SchemaColumn,
    SchemaSection,
    SemanticSection,
    SemanticTag,
    StatsSection,
)


def _make_report(
    row_count: int = 2000,
    quality_columns: list[ColumnQuality] | None = None,
    stats_columns: list[ColumnStats] | None = None,
    correlations: list[Correlation] | None = None,
    semantic_columns: list[SemanticTag] | None = None,
    duplicate_row_pct: float = 0.0,
) -> ProfileReport:
    schema_cols = [SchemaColumn(name="col", dtype="Float64", polars_dtype="Float64", nullable=True)]
    return ProfileReport(
        dataset_id=uuid.uuid4(),
        generated_at=datetime.now(timezone.utc),
        schema=SchemaSection(row_count=row_count, col_count=1, columns=schema_cols),
        stats=StatsSection(columns=stats_columns or []),
        quality=QualitySection(
            duplicate_row_pct=duplicate_row_pct,
            columns=quality_columns or [],
        ),
        relationships=RelationshipSection(correlations=correlations or []),
        semantic=SemanticSection(columns=semantic_columns or []),
        health=HealthSection(score=85.0, grade="B", components={}),
    )


@pytest.fixture
def large_report_high_null():
    """1000-row dataset where 'age' has 60% nulls — should yield HIGH severity."""
    return _make_report(
        row_count=1000,
        quality_columns=[ColumnQuality(name="age", null_pct=60.0, is_constant=False)],
        semantic_columns=[SemanticTag(name="age", semantic_type="NUMERIC", confidence=0.6)],
    )


@pytest.fixture
def small_report_low_null():
    """30-row dataset where 'age' has 8% nulls — LOW severity, low confidence."""
    return _make_report(
        row_count=30,
        quality_columns=[ColumnQuality(name="age", null_pct=8.0, is_constant=False)],
        semantic_columns=[SemanticTag(name="age", semantic_type="NUMERIC", confidence=0.6)],
    )


@pytest.fixture
def constant_column_report():
    return _make_report(
        row_count=500,
        quality_columns=[
            ColumnQuality(name="status", null_pct=0.0, is_constant=True),
            ColumnQuality(name="age", null_pct=5.0, is_constant=False),
        ],
    )


@pytest.fixture
def skewed_report():
    return _make_report(
        row_count=1500,
        stats_columns=[
            ColumnStats(
                name="salary",
                inferred_kind="numeric",
                numeric=NumericStats(
                    count=1500, mean=50000.0, std=30000.0,
                    min=10000.0, p25=30000.0, median=42000.0,
                    p75=60000.0, max=500000.0,
                    skew=3.5, kurtosis=12.0,
                ),
            )
        ],
        semantic_columns=[SemanticTag(name="salary", semantic_type="FINANCIAL", confidence=0.85)],
    )


@pytest.fixture
def strong_corr_report():
    return _make_report(
        row_count=2000,
        correlations=[
            Correlation(col_a="price", col_b="revenue", pearson=0.97, spearman=0.95),
            Correlation(col_a="age", col_b="tenure", pearson=0.55, spearman=0.53),
        ],
    )


@pytest.fixture
def duplicate_report():
    return _make_report(row_count=800, duplicate_row_pct=22.0)


@pytest.fixture
def imbalanced_report():
    return _make_report(
        row_count=1000,
        stats_columns=[
            ColumnStats(
                name="country",
                inferred_kind="categorical",
                categorical=CategoricalStats(
                    count=1000,
                    cardinality=5,
                    mode="US",
                    top_values=[("US", 920), ("UK", 40), ("DE", 20), ("FR", 15), ("AU", 5)],
                ),
            )
        ],
    )


@pytest.fixture
def outlier_report():
    return _make_report(
        row_count=1000,
        quality_columns=[
            ColumnQuality(name="revenue", null_pct=0.0, is_constant=False, outlier_pct=18.0),
        ],
        stats_columns=[
            ColumnStats(
                name="revenue",
                inferred_kind="numeric",
                numeric=NumericStats(count=1000, mean=5000.0, p25=1000.0, p75=8000.0),
            )
        ],
    )
