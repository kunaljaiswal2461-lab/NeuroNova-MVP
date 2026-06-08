"""Fixtures for viz layer tests.

Each fixture pairs a ProfileReport with a FindingsReport that mirrors what
Layer 3 would actually produce for that profile — this validates the
Layer 3 → Layer 4 integration contract.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding, FindingsReport
from agentic_engine.profiler.report import (
    ColumnQuality,
    ColumnStats,
    Correlation,
    DatetimeStats,
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _schema(cols: list[tuple[str, str]]) -> SchemaSection:
    return SchemaSection(
        row_count=1000,
        col_count=len(cols),
        columns=[SchemaColumn(name=n, dtype=t, polars_dtype=t, nullable=True) for n, t in cols],
    )


def _health() -> HealthSection:
    return HealthSection(score=80.0, grade="B", components={})


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def numeric_profile_and_findings():
    """Two numeric columns — should produce histograms and a heatmap."""
    did = uuid.uuid4()
    profile = ProfileReport(
        dataset_id=did,
        generated_at=_now(),
        schema=_schema([("salary", "Float64"), ("age", "Int64")]),
        stats=StatsSection(columns=[
            ColumnStats(
                name="salary", inferred_kind="numeric",
                numeric=NumericStats(count=1000, mean=55000.0, std=20000.0,
                                     min=20000.0, p25=40000.0, median=52000.0,
                                     p75=68000.0, max=200000.0, skew=2.1),
            ),
            ColumnStats(
                name="age", inferred_kind="numeric",
                numeric=NumericStats(count=1000, mean=35.0, std=10.0,
                                     min=18.0, p25=27.0, median=34.0,
                                     p75=43.0, max=65.0, skew=0.2),
            ),
        ]),
        quality=QualitySection(
            duplicate_row_pct=0.0,
            columns=[
                ColumnQuality(name="salary", null_pct=0.0, is_constant=False, outlier_pct=12.0),
                ColumnQuality(name="age", null_pct=0.0, is_constant=False, outlier_pct=1.0),
            ],
        ),
        relationships=RelationshipSection(correlations=[
            Correlation(col_a="salary", col_b="age", pearson=0.62, spearman=0.60),
        ]),
        semantic=SemanticSection(columns=[
            SemanticTag(name="salary", semantic_type="FINANCIAL", confidence=0.85),
            SemanticTag(name="age", semantic_type="NUMERIC", confidence=0.6),
        ]),
        health=_health(),
    )
    findings = FindingsReport(
        dataset_id=did,
        findings=[
            Finding(
                type=FindingType.SKEWED_DISTRIBUTION,
                severity=Severity.HIGH,
                confidence=0.88,
                column="salary",
                title="Skewed: salary",
                description="salary is right-skewed",
                evidence={"skew": 2.1},
            ),
            Finding(
                type=FindingType.HIGH_OUTLIER_DENSITY,
                severity=Severity.HIGH,
                confidence=0.82,
                column="salary",
                title="Outliers: salary",
                description="12% outliers",
                evidence={"outlier_pct": 12.0},
            ),
            Finding(
                type=FindingType.SEMANTIC_TAG,
                severity=Severity.LOW,
                confidence=0.85,
                column="salary",
                title="salary: FINANCIAL",
                description="",
                evidence={"semantic_type": "FINANCIAL"},
                semantic_context="FINANCIAL",
            ),
        ],
    )
    return profile, findings


@pytest.fixture
def categorical_profile_and_findings():
    """Categorical column with imbalance — bar + pie charts expected."""
    did = uuid.uuid4()
    profile = ProfileReport(
        dataset_id=did,
        generated_at=_now(),
        schema=_schema([("region", "Utf8")]),
        stats=StatsSection(columns=[
            ColumnStats(
                name="region", inferred_kind="categorical",
                categorical=CategoricalStats(
                    count=1000, cardinality=4, mode="US",
                    top_values=[("US", 880), ("UK", 70), ("DE", 30), ("FR", 20)],
                ),
            ),
        ]),
        quality=QualitySection(
            duplicate_row_pct=0.0,
            columns=[ColumnQuality(name="region", null_pct=0.0, is_constant=False)],
        ),
        relationships=RelationshipSection(correlations=[]),
        semantic=SemanticSection(columns=[
            SemanticTag(name="region", semantic_type="GEOGRAPHIC", confidence=0.85),
        ]),
        health=_health(),
    )
    imbal_finding = Finding(
        type=FindingType.IMBALANCED_DISTRIBUTION,
        severity=Severity.HIGH,
        confidence=0.91,
        column="region",
        title="Imbalanced: region",
        description="US is 88%",
        evidence={"dominant_value": "US", "dominant_pct": 88.0},
    )
    findings = FindingsReport(dataset_id=did, findings=[imbal_finding])
    return profile, findings


@pytest.fixture
def corr_profile_and_findings():
    """Two strongly correlated columns — scatter chart expected."""
    did = uuid.uuid4()
    corr_finding = Finding(
        type=FindingType.STRONG_CORRELATION,
        severity=Severity.HIGH,
        confidence=0.95,
        column=None,
        title="price ↔ revenue",
        description="r=0.97",
        evidence={
            "col_a": "price", "col_b": "revenue",
            "pearson": 0.97, "spearman": 0.95,
            "best_method": "pearson", "best_r": 0.97,
        },
    )
    profile = ProfileReport(
        dataset_id=did,
        generated_at=_now(),
        schema=_schema([("price", "Float64"), ("revenue", "Float64")]),
        stats=StatsSection(columns=[
            ColumnStats(name="price", inferred_kind="numeric",
                        numeric=NumericStats(count=1000, mean=100.0)),
            ColumnStats(name="revenue", inferred_kind="numeric",
                        numeric=NumericStats(count=1000, mean=500.0)),
        ]),
        quality=QualitySection(
            duplicate_row_pct=0.0,
            columns=[
                ColumnQuality(name="price", null_pct=0.0, is_constant=False),
                ColumnQuality(name="revenue", null_pct=0.0, is_constant=False),
            ],
        ),
        relationships=RelationshipSection(correlations=[
            Correlation(col_a="price", col_b="revenue", pearson=0.97, spearman=0.95),
        ]),
        semantic=SemanticSection(columns=[
            SemanticTag(name="price", semantic_type="FINANCIAL", confidence=0.85),
            SemanticTag(name="revenue", semantic_type="FINANCIAL", confidence=0.85),
        ]),
        health=_health(),
    )
    findings = FindingsReport(dataset_id=did, findings=[corr_finding])
    return profile, findings


@pytest.fixture
def constant_and_id_profile_and_findings():
    """Constant + ID column — both should be skipped in all charts."""
    did = uuid.uuid4()
    profile = ProfileReport(
        dataset_id=did,
        generated_at=_now(),
        schema=_schema([("id", "Int64"), ("status", "Utf8"), ("amount", "Float64")]),
        stats=StatsSection(columns=[
            ColumnStats(name="id", inferred_kind="numeric",
                        numeric=NumericStats(count=1000, mean=500.0)),
            ColumnStats(name="status", inferred_kind="categorical",
                        categorical=CategoricalStats(count=1000, cardinality=1, mode="active")),
            ColumnStats(name="amount", inferred_kind="numeric",
                        numeric=NumericStats(count=1000, mean=200.0, p25=100.0, p75=300.0)),
        ]),
        quality=QualitySection(
            duplicate_row_pct=0.0,
            columns=[
                ColumnQuality(name="id", null_pct=0.0, is_constant=False),
                ColumnQuality(name="status", null_pct=0.0, is_constant=True),
                ColumnQuality(name="amount", null_pct=0.0, is_constant=False),
            ],
        ),
        relationships=RelationshipSection(correlations=[]),
        semantic=SemanticSection(columns=[
            SemanticTag(name="id", semantic_type="IDENTIFIER", confidence=0.9),
            SemanticTag(name="status", semantic_type="CATEGORICAL", confidence=0.7),
            SemanticTag(name="amount", semantic_type="FINANCIAL", confidence=0.85),
        ]),
        health=_health(),
    )
    findings = FindingsReport(
        dataset_id=did,
        findings=[
            Finding(
                type=FindingType.CONSTANT_COLUMN, severity=Severity.HIGH,
                confidence=0.99, column="status",
                title="Constant: status", description="", evidence={},
            ),
            Finding(
                type=FindingType.ID_COLUMN_DETECTED, severity=Severity.LOW,
                confidence=0.9, column="id",
                title="ID: id", description="", evidence={"detection_method": "semantic"},
            ),
        ],
    )
    return profile, findings


@pytest.fixture
def timeseries_profile_and_findings():
    """Datetime + numeric columns — timeseries chart expected."""
    did = uuid.uuid4()
    profile = ProfileReport(
        dataset_id=did,
        generated_at=_now(),
        schema=_schema([("created_at", "Datetime"), ("revenue", "Float64")]),
        stats=StatsSection(columns=[
            ColumnStats(
                name="created_at", inferred_kind="datetime",
                datetime=DatetimeStats(count=1000, range_days=180.0),
            ),
            ColumnStats(
                name="revenue", inferred_kind="numeric",
                numeric=NumericStats(count=1000, mean=500.0),
            ),
        ]),
        quality=QualitySection(
            duplicate_row_pct=0.0,
            columns=[
                ColumnQuality(name="created_at", null_pct=0.0, is_constant=False),
                ColumnQuality(name="revenue", null_pct=0.0, is_constant=False),
            ],
        ),
        relationships=RelationshipSection(correlations=[]),
        semantic=SemanticSection(columns=[
            SemanticTag(name="created_at", semantic_type="TEMPORAL", confidence=0.95),
            SemanticTag(name="revenue", semantic_type="FINANCIAL", confidence=0.85),
        ]),
        health=_health(),
    )
    findings = FindingsReport(dataset_id=did, findings=[])
    return profile, findings
