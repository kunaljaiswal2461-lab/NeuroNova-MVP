"""Confidence scoring helpers.

confidence = sample_factor × effect_factor

sample_factor: scales from 0.5 at MIN_ROWS to 1.0 at HIGH_ROWS.
  Small datasets make every statistical observation less certain.

effect_factor: scales from 0.6 at min_effect to 1.0 at max_effect.
  Weak effects near the detection threshold are less trustworthy than
  extreme ones.

Both factors are clamped to [0,1] and multiplied, giving a final
score in [0.0, 1.0]. The product is rounded to 3 decimal places.
"""
from __future__ import annotations

_MIN_ROWS = 50      # below this, confidence is heavily penalised
_HIGH_ROWS = 1_000  # above this, sample size stops penalising confidence


def _sample_factor(row_count: int) -> float:
    if row_count <= _MIN_ROWS:
        return 0.5
    if row_count >= _HIGH_ROWS:
        return 1.0
    return 0.5 + 0.5 * (row_count - _MIN_ROWS) / (_HIGH_ROWS - _MIN_ROWS)


def _effect_factor(effect: float, min_effect: float, max_effect: float) -> float:
    """Normalise effect to [0,1] and map to [0.6, 1.0]."""
    if max_effect <= min_effect:
        return 0.8
    norm = min(1.0, max(0.0, (effect - min_effect) / (max_effect - min_effect)))
    return 0.6 + 0.4 * norm


def confidence_score(
    row_count: int,
    effect: float,
    min_effect: float,
    max_effect: float,
) -> float:
    """Return a confidence score in [0.0, 1.0].

    Args:
        row_count: total dataset rows.
        effect: the raw magnitude of the observation (e.g. null_pct, |correlation|).
        min_effect: the detection threshold — effect at or below this → lowest confidence.
        max_effect: a "saturating" value — effect at or above this → highest confidence.
    """
    sf = _sample_factor(row_count)
    ef = _effect_factor(effect, min_effect, max_effect)
    return round(sf * ef, 3)


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))