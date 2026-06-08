"""Data quality profile — nulls, duplicates, constants, IQR outliers."""
from __future__ import annotations

import polars as pl

from agentic_engine.profiler.report import ColumnQuality, QualitySection


def _duplicate_row_pct(df: pl.DataFrame) -> float:
    if df.height == 0:
        return 0.0
    unique_rows = df.unique().height
    return round(100.0 * (df.height - unique_rows) / df.height, 4)


def _iqr_outlier_pct(series: pl.Series) -> float | None:
    non_null = series.drop_nulls()
    n = non_null.len()
    if n < 4:
        return None
    q1 = non_null.quantile(0.25)
    q3 = non_null.quantile(0.75)
    if q1 is None or q3 is None:
        return None
    iqr = q3 - q1
    if iqr == 0:
        return 0.0
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    out = non_null.filter((non_null < lo) | (non_null > hi)).len()
    return round(100.0 * out / n, 4)


def profile_quality(df: pl.DataFrame) -> QualitySection:
    rows = df.height
    cols: list[ColumnQuality] = []

    for name in df.columns:
        series = df.get_column(name)
        nulls = series.null_count()
        null_pct = round(100.0 * nulls / rows, 4) if rows else 0.0

        non_null = series.drop_nulls()
        is_constant = non_null.len() > 0 and non_null.n_unique() == 1

        outlier_pct: float | None = None
        if series.dtype.is_numeric() and not series.dtype.is_temporal():
            outlier_pct = _iqr_outlier_pct(series)

        cols.append(
            ColumnQuality(
                name=name,
                null_pct=null_pct,
                is_constant=is_constant,
                outlier_pct=outlier_pct,
            )
        )

    return QualitySection(
        duplicate_row_pct=_duplicate_row_pct(df),
        columns=cols,
    )
