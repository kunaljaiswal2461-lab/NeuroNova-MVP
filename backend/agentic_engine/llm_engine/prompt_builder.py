"""Build compact, LLM-safe context payloads from Layer 2/3 reports.

The two source reports (``ProfileReport``, ``FindingsReport``) are too
verbose to send to the LLM verbatim — they contain redundant fields,
floats with excessive precision, and per-column noise that would balloon
token counts. This module produces three lean dictionaries, one per
prompt, each containing only the signals the prompt actually needs.

Hard invariant: **no raw row-level data ever leaves this module**. Only
schema, statistics, semantic tags, and findings.
"""
from __future__ import annotations

from typing import Any

from agentic_engine.findings.finding_types import Severity
from agentic_engine.findings.models import Finding, FindingsReport
from agentic_engine.profiler.report import ProfileReport


# Caps that keep prompts inside a sensible token budget. These are
# conservative — even a 100-column dataset stays well under 8K tokens.
_MAX_COLUMNS_IN_CONTEXT = 40
_MAX_TOP_VALUES_PER_COL = 5
_MAX_CORRELATIONS_IN_CONTEXT = 10
_MAX_FINDING_TITLES_PER_SEVERITY = 20


# ── public entry points ──────────────────────────────────────────────────────

def build_dataset_context(profile: ProfileReport, findings: FindingsReport) -> dict[str, Any]:
    """Shared, compact snapshot used by every prompt.

    Returns a dict ready for ``json.dumps`` — every value is a primitive,
    list, or nested dict, with floats rounded to 3 decimal places.
    """
    return {
        "shape": {
            "rows": profile.schema_.row_count,
            "cols": profile.schema_.col_count,
        },
        "health": {
            "score": round(profile.health.score, 1),
            "grade": profile.health.grade,
        },
        "duplicate_row_pct": round(profile.quality.duplicate_row_pct, 2),
        "columns": _summarise_columns(profile),
        "top_correlations": _summarise_correlations(profile),
        "findings_summary": _summarise_findings(findings),
    }


def select_findings_for_explanation(findings: FindingsReport) -> list[Finding]:
    """Pick the findings worth a full LLM explanation.

    All HIGH/MEDIUM are included. LOW findings are skipped — they are
    structurally captured by the Layer 3 report itself and explaining each
    one would inflate cost without informational gain.
    """
    return [
        f for f in findings.findings
        if f.severity in (Severity.HIGH, Severity.MEDIUM)
    ]


def serialise_findings_for_prompt(findings: list[Finding]) -> list[dict[str, Any]]:
    """Convert findings into a terse, JSON-safe shape for the LLM."""
    return [
        {
            "finding_id": str(f.finding_id),
            "type": f.type.value,
            "severity": f.severity.value,
            "confidence": f.confidence,
            "column": f.column,
            "title": f.title,
            "description": f.description,
            "evidence": _trim_evidence(f.evidence),
            "semantic_context": f.semantic_context,
        }
        for f in findings
    ]


# ── helpers ──────────────────────────────────────────────────────────────────

def _summarise_columns(profile: ProfileReport) -> list[dict[str, Any]]:
    """One terse row per column — name, kind, semantic, key quality flags."""
    quality_by_name = {q.name: q for q in profile.quality.columns}
    semantic_by_name = {s.name: s for s in profile.semantic.columns}
    stats_by_name = {s.name: s for s in profile.stats.columns}

    summaries: list[dict[str, Any]] = []
    for col in profile.schema_.columns[:_MAX_COLUMNS_IN_CONTEXT]:
        q = quality_by_name.get(col.name)
        s = semantic_by_name.get(col.name)
        st = stats_by_name.get(col.name)

        entry: dict[str, Any] = {
            "name": col.name,
            "dtype": col.dtype,
        }
        if st is not None:
            entry["kind"] = st.inferred_kind
        if s is not None:
            entry["semantic_type"] = s.semantic_type
        if q is not None:
            entry["null_pct"] = round(q.null_pct, 2)
            if q.is_constant:
                entry["is_constant"] = True
            if q.outlier_pct is not None and q.outlier_pct > 0:
                entry["outlier_pct"] = round(q.outlier_pct, 2)
        if st is not None:
            stats_excerpt = _stats_excerpt(st)
            if stats_excerpt:
                entry["stats"] = stats_excerpt
        summaries.append(entry)
    return summaries


def _stats_excerpt(col_stats: Any) -> dict[str, Any] | None:
    """Extract the few stat fields the LLM actually benefits from seeing."""
    if col_stats.numeric is not None:
        n = col_stats.numeric
        return _drop_none({
            "mean": _round_or_none(n.mean),
            "median": _round_or_none(n.median),
            "min": _round_or_none(n.min),
            "max": _round_or_none(n.max),
            "skew": _round_or_none(n.skew),
        })
    if col_stats.categorical is not None:
        c = col_stats.categorical
        top = [
            {"value": str(v), "count": int(cnt)}
            for v, cnt in (c.top_values or [])[:_MAX_TOP_VALUES_PER_COL]
        ]
        return _drop_none({
            "cardinality": c.cardinality,
            "mode": c.mode,
            "top_values": top or None,
        })
    if col_stats.datetime_ is not None:
        d = col_stats.datetime_
        return _drop_none({
            "min": d.min.isoformat() if d.min else None,
            "max": d.max.isoformat() if d.max else None,
            "range_days": _round_or_none(d.range_days),
        })
    return None


def _summarise_correlations(profile: ProfileReport) -> list[dict[str, Any]]:
    """Strongest |pearson| pairs only — the rest are noise to the LLM."""
    ranked = sorted(
        profile.relationships.correlations,
        key=lambda c: abs(c.pearson or 0.0),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for c in ranked[:_MAX_CORRELATIONS_IN_CONTEXT]:
        if c.pearson is None and c.spearman is None:
            continue
        out.append({
            "col_a": c.col_a,
            "col_b": c.col_b,
            "pearson": _round_or_none(c.pearson),
            "spearman": _round_or_none(c.spearman),
        })
    return out


def _summarise_findings(findings: FindingsReport) -> dict[str, Any]:
    """Severity counts + a flat list of titles for narrative anchoring."""
    by_sev: dict[str, list[str]] = {"HIGH": [], "MEDIUM": [], "LOW": []}
    for f in findings.findings:
        bucket = by_sev.setdefault(f.severity.value, [])
        if len(bucket) < _MAX_FINDING_TITLES_PER_SEVERITY:
            bucket.append(f.title)

    return {
        "total": findings.count,
        "high": len([f for f in findings.findings if f.severity == Severity.HIGH]),
        "medium": len([f for f in findings.findings if f.severity == Severity.MEDIUM]),
        "low": len([f for f in findings.findings if f.severity == Severity.LOW]),
        "titles_by_severity": by_sev,
    }


def _trim_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Round floats and strip nulls; structurally preserve everything else."""
    trimmed: dict[str, Any] = {}
    for key, value in evidence.items():
        if value is None:
            continue
        if isinstance(value, float):
            trimmed[key] = round(value, 4)
        else:
            trimmed[key] = value
    return trimmed


def _round_or_none(value: float | None, ndigits: int = 3) -> float | None:
    return None if value is None else round(value, ndigits)


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}
