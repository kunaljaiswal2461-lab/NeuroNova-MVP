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
from agentic_engine.profiler.confidence import confidence_score
from agentic_engine.profiler.report import StatsSection


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


_SKEW_MIN_EFFECT = 1.0
_SKEW_MAX_EFFECT = 5.0
_SKEW_MAX_PENALTY = 40.0


def _skew_penalty(quality: QualitySection, stats: StatsSection, row_count: int) -> float:
    """Deduct cleanliness points for skewed numeric columns, scaled by confidence."""
    skew_by_name = {
        c.name: c.numeric.skew
        for c in stats.columns
        if c.numeric is not None and c.numeric.skew is not None
    }
    if not skew_by_name:
        return 0.0

    total_penalty = 0.0
    n = len(skew_by_name)
    for name, skew in skew_by_name.items():
        abs_skew = abs(skew)
        if abs_skew <= _SKEW_MIN_EFFECT:
            continue
        conf = confidence_score(
            row_count=row_count,
            effect=abs_skew,
            min_effect=_SKEW_MIN_EFFECT,
            max_effect=_SKEW_MAX_EFFECT,
        )
        severity = min(1.0, (abs_skew - _SKEW_MIN_EFFECT) / (_SKEW_MAX_EFFECT - _SKEW_MIN_EFFECT))
        total_penalty += severity * conf * (_SKEW_MAX_PENALTY / n)

    return min(_SKEW_MAX_PENALTY, total_penalty)


def _cleanliness(quality: QualitySection, stats: StatsSection, row_count: int) -> float:
    outlier_cols = [c.outlier_pct for c in quality.columns if c.outlier_pct is not None]
    mean_outlier = sum(outlier_cols) / len(outlier_cols) if outlier_cols else 0.0
    base = 100.0 - mean_outlier
    base -= _skew_penalty(quality, stats, row_count)
    return max(0.0, base)


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
    stats: StatsSection,
    settings: Settings,
) -> HealthSection:
    components = {
        "completeness": round(_completeness(quality), 2),
        "uniqueness": round(_uniqueness(quality), 2),
        "consistency": round(_consistency(quality, schema), 2),
        "cleanliness": round(_cleanliness(quality, stats, schema.row_count), 2),
    }
    score = round(sum(components[k] * _WEIGHTS[k] for k in components), 2)
    return HealthSection(score=score, grade=_grade(score, settings), components=components)
