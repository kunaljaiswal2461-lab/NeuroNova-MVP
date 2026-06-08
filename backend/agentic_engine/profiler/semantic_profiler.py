"""Heuristic semantic column typing.

This is intentionally rule-based for MVP — column-name keyword matches plus a
small handful of value-pattern probes. Each tag carries a 0..1 confidence so
later layers (Findings, LLM) can weight them.
"""
from __future__ import annotations

import re
from typing import Iterable

import polars as pl

from agentic_engine.profiler.report import SemanticSection, SemanticTag, SemanticType


_NAME_PATTERNS: list[tuple[SemanticType, float, tuple[str, ...]]] = [
    ("IDENTIFIER", 0.9, ("id", "uuid", "guid", "_pk", "key")),
    ("FINANCIAL", 0.85, ("price", "cost", "revenue", "amount", "salary", "balance",
                         "profit", "loss", "spend", "income", "fee", "discount")),
    ("GEOGRAPHIC", 0.85, ("country", "state", "city", "region", "zip", "postal",
                          "address", "lat", "latitude", "lon", "lng", "longitude")),
    ("TEMPORAL", 0.8,    ("date", "time", "_at", "timestamp", "created", "updated")),
    ("EMAIL", 0.9,       ("email",)),
    ("URL", 0.8,         ("url", "website", "homepage", "link")),
    ("PHONE", 0.85,      ("phone", "mobile", "tel")),
    ("BOOLEAN", 0.7,     ("is_", "has_", "flag")),
]

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_PHONE_RE = re.compile(r"^\+?[\d\s\-().]{7,}$")

_SAMPLE_SIZE = 200  # how many non-null string values to probe per column


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower())


def _name_hits(col_name: str) -> tuple[SemanticType, float] | None:
    norm = _normalise(col_name)
    tokens = norm.split("_")
    for stype, conf, keywords in _NAME_PATTERNS:
        for kw in keywords:
            if kw.endswith("_") or kw.startswith("_"):
                if any(tok.startswith(kw.strip("_")) for tok in tokens):
                    return stype, conf
            elif kw in tokens or kw in norm:
                return stype, conf
    return None


def _value_signature(values: Iterable[str]) -> tuple[SemanticType, float] | None:
    sample = [v for v in values if v]
    if not sample:
        return None
    n = len(sample)
    emails = sum(1 for v in sample if _EMAIL_RE.match(v))
    urls = sum(1 for v in sample if _URL_RE.match(v))
    phones = sum(1 for v in sample if _PHONE_RE.match(v))

    for stype, hits in (("EMAIL", emails), ("URL", urls), ("PHONE", phones)):
        if hits / n >= 0.8:
            return stype, round(0.6 + 0.35 * (hits / n), 3)  # 0.88..0.95
    return None


def _dtype_fallback(series: pl.Series) -> tuple[SemanticType, float]:
    dt = series.dtype
    if dt == pl.Boolean:
        return "BOOLEAN", 0.95
    if dt.is_temporal():
        return "TEMPORAL", 0.95
    if dt.is_numeric():
        return "NUMERIC", 0.6
    if dt == pl.String or dt == pl.Utf8:
        # categorical-ish vs free text decided by cardinality
        non_null = series.drop_nulls()
        if non_null.len() and non_null.n_unique() <= 50:
            return "CATEGORICAL", 0.6
        return "TEXT", 0.5
    return "UNKNOWN", 0.3


def profile_semantic(df: pl.DataFrame) -> SemanticSection:
    tags: list[SemanticTag] = []
    for col in df.columns:
        series = df.get_column(col)

        name_hit = _name_hits(col)
        value_hit: tuple[SemanticType, float] | None = None

        if series.dtype == pl.String or series.dtype == pl.Utf8:
            sample_values = (
                series.drop_nulls().head(_SAMPLE_SIZE).cast(pl.String).to_list()
            )
            value_hit = _value_signature(sample_values)

        # priority: value-pattern > name > dtype fallback
        chosen: tuple[SemanticType, float]
        if value_hit is not None:
            chosen = value_hit
        elif name_hit is not None:
            chosen = name_hit
        else:
            chosen = _dtype_fallback(series)

        tags.append(SemanticTag(name=col, semantic_type=chosen[0], confidence=chosen[1]))

    return SemanticSection(columns=tags)
