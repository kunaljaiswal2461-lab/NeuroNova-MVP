"""Pure selection math and value rules for sample values (SPEC-07 B2). No I/O."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .pii import any_value_is_pii

# Part 3's render limits (a test pins them to ``schema_context.literals``). Not imported: that package pulls in
# ``catalog_service``, which imports this one.
SAMPLE_VALUE_LIMIT = 64
MAX_SAMPLE_VALUES = 20

# ``pg_stats.most_common_freqs`` is float4, so a true 0.95 can sum to 0.94999994.
COVERAGE_EPSILON = 1e-6
# Stored values keep one code point past the render limit: ``untrusted_literal`` cuts anything longer than 64
# code points to 63 + "…", which is how a truncated value is marked (SPEC-07 B2 step 4).
STORED_VALUE_CODE_POINTS = SAMPLE_VALUE_LIMIT + 1

SKIP_REASONS = (
    "excluded_by_owner", "pii_name", "pii_values", "high_cardinality", "low_coverage", "no_statistics",
    "not_text", "error",
)
TEXT_NATIVE_TYPES = frozenset({"text", "varchar", "bpchar"})


@dataclass(frozen=True)
class Sample:
    """The decision for one column: either ``values`` (kept) or ``skip`` (a reason from ``SKIP_REASONS``)."""

    values: tuple[tuple[str, float], ...] = ()      # (value, frequency), final order, final truncation
    method: str = ""
    coverage: float = 0.0
    skip: str | None = None


def skipped(reason: str) -> Sample:
    assert reason in SKIP_REASONS
    return Sample(skip=reason)


def estimated_distinct(n_distinct: float | None, reltuples: float | None) -> float | None:
    """``n_distinct`` > 0 is a count; < 0 is minus a fraction of the row count; unknown row count -> ``None``."""
    if n_distinct is None or isinstance(n_distinct, bool) or not math.isfinite(n_distinct):
        return None
    if n_distinct >= 0:
        return float(n_distinct)
    if reltuples is None or not math.isfinite(reltuples) or reltuples < 0:    # reltuples = -1: never analysed
        return None
    return -n_distinct * reltuples


def finalize_values(pairs: Sequence[tuple[str | None, float]]) -> tuple[tuple[str, float], ...]:
    """Value rules (B2 step 4): drop NULL, order by frequency then code point, cap at 20, truncate.

    Values are otherwise kept exactly as stored: ``"enterprise "`` keeps its trailing space. Equal values (which
    can only appear after truncation) are not merged; their order is stable.
    """
    kept = [(v, f) for v, f in pairs if v is not None]
    kept.sort(key=lambda item: (-item[1], item[0]))          # str comparison is by code point
    return tuple((v[:STORED_VALUE_CODE_POINTS], float(f)) for v, f in kept[:MAX_SAMPLE_VALUES])


def decide_from_pg_stats(
    *,
    n_distinct: float | None,
    null_frac: float | None,
    vals: Sequence[str | None] | None,
    freqs: Sequence[float] | None,
    reltuples: float | None,
    max_distinct: int,
    min_coverage: float,
) -> Sample:
    est = estimated_distinct(n_distinct, reltuples)
    if est is None:
        return skipped("no_statistics")
    if est > max_distinct:
        return skipped("high_cardinality")
    if not vals or not freqs or len(vals) != len(freqs):
        return skipped("low_coverage")
    nulls = 0.0 if null_frac is None else float(null_frac)
    coverage = float(sum(freqs)) + nulls
    if coverage + COVERAGE_EPSILON < min_coverage:
        return skipped("low_coverage")
    pairs = list(zip(vals, freqs))
    if _has_pii(pairs):
        return skipped("pii_values")
    return Sample(values=finalize_values(pairs), method="pg_stats", coverage=min(coverage, 1.0))


def decide_from_exact(
    *, groups: Sequence[tuple[str | None, int]], max_distinct: int
) -> Sample:
    """``groups`` are the rows of ``GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT max_distinct+1`` (NULL group included)."""
    non_null = [(v, n) for v, n in groups if v is not None]
    if len(non_null) > max_distinct:
        return skipped("high_cardinality")
    total = sum(n for _, n in groups)
    if total <= 0 or not non_null:
        return skipped("low_coverage")                 # an empty table or an all-NULL column has nothing to show
    pairs = [(v, n / total) for v, n in non_null]
    if _has_pii(pairs):
        return skipped("pii_values")
    return Sample(values=finalize_values(pairs), method="exact", coverage=1.0)


def _has_pii(pairs: Sequence[tuple[str | None, float]]) -> bool:
    """I1: ANY collected value (before the 20 cap and the truncation) that looks like personal data drops the column."""
    return any_value_is_pii(v for v, _ in pairs if v is not None)
