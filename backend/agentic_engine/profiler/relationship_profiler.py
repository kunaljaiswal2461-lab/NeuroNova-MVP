"""Numeric pairwise correlations — Pearson + Spearman.

Spec explicitly excludes multicollinearity / causal inference for MVP.
"""
from __future__ import annotations

import math

import polars as pl
from scipy import stats as sp_stats

from agentic_engine.profiler.report import Correlation, RelationshipSection


_MIN_PAIRS = 5            # need this many non-null pairs to compute either coef
_MAX_NUMERIC_COLS = 20    # cap O(n^2) explosion on wide datasets


def _safe(x: object) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 6)


def profile_relationships(df: pl.DataFrame) -> RelationshipSection:
    numeric_cols = [
        c for c in df.columns
        if df.get_column(c).dtype.is_numeric() and not df.get_column(c).dtype.is_temporal()
    ][:_MAX_NUMERIC_COLS]

    out: list[Correlation] = []
    for i, a in enumerate(numeric_cols):
        for b in numeric_cols[i + 1 :]:
            pair = df.select([a, b]).drop_nulls()
            if pair.height < _MIN_PAIRS:
                continue
            xa = pair.get_column(a).to_numpy()
            xb = pair.get_column(b).to_numpy()

            try:
                pearson = _safe(sp_stats.pearsonr(xa, xb).statistic)
            except Exception:
                pearson = None
            try:
                spearman = _safe(sp_stats.spearmanr(xa, xb).statistic)
            except Exception:
                spearman = None

            if pearson is None and spearman is None:
                continue
            out.append(
                Correlation(col_a=a, col_b=b, pearson=pearson, spearman=spearman)
            )

    return RelationshipSection(correlations=out)
