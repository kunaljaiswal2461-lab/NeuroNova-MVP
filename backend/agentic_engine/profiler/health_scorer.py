"""Dataset health score (0..100) with A/B/C/D/F letter grade.

Composite of four equally-weighted components (each 0..100):

  completeness  — 100 - mean(null_pct across cols)
  uniqueness    — 100 - duplicate_row_pct
  consistency   — penalty for constant columns (zero-information)
  cleanliness   — 100 - mean(outlier_pct across numeric cols)

Grade thresholds come from Settings (health_grade_a..d).
"""
from __future__ import annotations

from app.core.config import Settings
from agentic_engine.profiler.report import (
    HealthGrade,
    HealthSection,
    QualitySection,
    SchemaSection,
)


_WEIGHTS = {
    "completeness": 0.30,
    "uniqueness": 0.20,
    "consistency": 0.20,
    "cleanliness": 0.30,
}


def _completeness(quality: QualitySection) -> float:
    if not quality.columns:
        return 100.0
    mean_null_pct = sum(c.null_pct for c in quality.columns) / len(quality.columns)
    return max(0.0, 100.0 - mean_null_pct)


def _uniqueness(quality: QualitySection) -> float:
    return max(0.0, 100.0 - quality.duplicate_row_pct)


def _consistency(quality: QualitySection, schema: SchemaSection) -> float:
    cols = schema.col_count or 1
    constant_cols = sum(1 for c in quality.columns if c.is_constant)
    return max(0.0, 100.0 - 100.0 * constant_cols / cols)


def _cleanliness(quality: QualitySection) -> float:
    outlier_cols = [c.outlier_pct for c in quality.columns if c.outlier_pct is not None]
    if not outlier_cols:
        return 100.0
    mean_outlier = sum(outlier_cols) / len(outlier_cols)
    return max(0.0, 100.0 - mean_outlier)


def _grade(score: float, settings: Settings) -> HealthGrade:
    if score >= settings.health_grade_a:
        return "A"
    if score >= settings.health_grade_b:
        return "B"
    if score >= settings.health_grade_c:
        return "C"
    if score >= settings.health_grade_d:
        return "D"
    return "F"


def compute_health(
    schema: SchemaSection,
    quality: QualitySection,
    settings: Settings,
) -> HealthSection:
    components = {
        "completeness": round(_completeness(quality), 2),
        "uniqueness": round(_uniqueness(quality), 2),
        "consistency": round(_consistency(quality, schema), 2),
        "cleanliness": round(_cleanliness(quality), 2),
    }
    score = round(sum(components[k] * _WEIGHTS[k] for k in components), 2)
    return HealthSection(score=score, grade=_grade(score, settings), components=components)
