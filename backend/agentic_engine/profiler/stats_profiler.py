"""Per-column statistical profile."""
from __future__ import annotations

import numpy as np 
import math
from typing import Literal

import polars as pl
from scipy import stats as sp_stats

from agentic_engine.profiler.report import (
    CategoricalStats,
    ColumnStats,
    DatetimeStats,
    NumericStats,
    StatsSection,
)


ColumnKind = Literal["numeric", "categorical", "datetime", "boolean", "text", "other"]

_TOP_K = 10
_CATEGORICAL_CARDINALITY_CEILING = 50  # string col is "categorical" if distinct <= this


def _infer_kind(series: pl.Series) -> ColumnKind:
    dtype = series.dtype
    if dtype.is_numeric() and not dtype.is_temporal():
        return "numeric"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype.is_temporal():
        return "datetime"
    if dtype == pl.String or dtype == pl.Utf8:
        distinct = series.n_unique()
        if distinct <= _CATEGORICAL_CARDINALITY_CEILING:
            return "categorical"
        return "text"
    return "other"


def _safe_float(x: object) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _numeric_stats(series: pl.Series) -> NumericStats:
    non_null = series.drop_nulls()
    count = non_null.len()
    if count == 0:
        return NumericStats(count=0)

    quartiles = non_null.quantile(0.25), non_null.quantile(0.5), non_null.quantile(0.75)

    skew: float | None = None
    kurt: float | None = None
    if count >= 3:
        arr = non_null.to_numpy()
        try:
            skew = _safe_float(sp_stats.skew(arr, bias=False))
            kurt = _safe_float(sp_stats.kurtosis(arr, bias=False))
        except Exception:
            skew, kurt = None, None

        try:
            n_bins=max(10,min(50,int(count*0.05)))
            counts, edges=np.histogram(arr,bins=n_bins)
            bin_edges=[float(e) for e in edges]
            bin_counts=[int(c) for c in counts]
        except:
            bin_edges,bin_counts=None,None

    return NumericStats(
        count=count,
        mean=_safe_float(non_null.mean()),
        std=_safe_float(non_null.std()) if count > 1 else None,
        min=_safe_float(non_null.min()),
        p25=_safe_float(quartiles[0]),
        median=_safe_float(quartiles[1]),
        p75=_safe_float(quartiles[2]),
        max=_safe_float(non_null.max()),
        skew=skew,
        kurtosis=kurt,
        bin_edges=bin_edges,
        bin_counts=bin_counts,
    )


def _categorical_stats(series: pl.Series) -> CategoricalStats:
    non_null = series.drop_nulls()
    count = non_null.len()
    if count == 0:
        return CategoricalStats(count=0, cardinality=0)

    vc = non_null.value_counts(sort=True).head(_TOP_K)
    value_col = vc.columns[0]
    count_col = "count" if "count" in vc.columns else vc.columns[1]
    top = [(str(row[value_col]), int(row[count_col])) for row in vc.iter_rows(named=True)]

    return CategoricalStats(
        count=count,
        cardinality=non_null.n_unique(),
        mode=top[0][0] if top else None,
        top_values=top,
    )


def _to_datetime(x: object) -> "datetime.datetime | None":
    """Coerce polars temporal min/max output into a `datetime`. Returns None for
    `datetime.time` / `timedelta` since those can't be represented as a wall-clock
    moment."""
    import datetime as _dt
    if x is None:
        return None
    if isinstance(x, _dt.datetime):
        return x
    if isinstance(x, _dt.date):
        return _dt.datetime(x.year, x.month, x.day)
    return None


def _datetime_stats(series: pl.Series) -> DatetimeStats:
    non_null = series.drop_nulls()
    count = non_null.len()
    if count == 0:
        return DatetimeStats(count=0)

    raw_lo = non_null.min()
    raw_hi = non_null.max()

    range_days: float | None = None
    if raw_lo is not None and raw_hi is not None:
        try:
            delta = raw_hi - raw_lo
            secs = delta.total_seconds() if hasattr(delta, "total_seconds") else None
            if secs is not None:
                range_days = secs / 86400.0
        except Exception:
            range_days = None

    return DatetimeStats(
        count=count,
        min=_to_datetime(raw_lo),
        max=_to_datetime(raw_hi),
        range_days=range_days,
    )


def profile_stats(df: pl.DataFrame) -> StatsSection:
    out: list[ColumnStats] = []
    for col in df.columns:
        series = df.get_column(col)
        kind = _infer_kind(series)

        numeric = _numeric_stats(series) if kind == "numeric" else None
        categorical = (
            _categorical_stats(series) if kind in ("categorical", "boolean", "text") else None
        )
        datetime_ = _datetime_stats(series) if kind == "datetime" else None

        out.append(
            ColumnStats(
                name=col,
                inferred_kind=kind,
                numeric=numeric,
                categorical=categorical,
                datetime=datetime_,
            )
        )
    return StatsSection(columns=out)
