"""Findings orchestrator.

Runs every extractor against a ProfileReport, then passes the accumulated
findings to the recommendations extractor for cross-finding insights.
Returns a FindingsReport ready for persistence and vector indexing.
"""
from __future__ import annotations

import uuid

from app.core.logging import get_logger
from agentic_engine.findings.extractors import (
    constant_column,
    correlations,
    distribution,
    duplicates,
    id_column,
    nullability,
    outliers,
    recommendations,
    semantic_tag,
)
from agentic_engine.findings.models import Finding, FindingsReport
from agentic_engine.profiler.report import ProfileReport


logger = get_logger("findings.builder")

# Ordered extractors — recommendations runs last and sees all prior findings.
_EXTRACTORS = [
    nullability,
    constant_column,
    id_column,
    duplicates,
    distribution,
    outliers,
    correlations,
    semantic_tag,
]


def build_findings(report: ProfileReport) -> FindingsReport:
    """Run all extractors and return a FindingsReport."""
    all_findings: list[Finding] = []

    for extractor in _EXTRACTORS:
        try:
            found = extractor.extract(report)
            all_findings.extend(found)
            logger.debug(
                "findings.extractor_done",
                extractor=extractor.__name__.split(".")[-1],
                count=len(found),
            )
        except Exception:
            logger.exception(
                "findings.extractor_error",
                extractor=extractor.__name__,
            )

    # Recommendations see all prior findings as cross-extractor signals.
    try:
        recs = recommendations.extract_from_findings(report, all_findings)
        all_findings.extend(recs)
        logger.debug("findings.recommendations_done", count=len(recs))
    except Exception:
        logger.exception("findings.recommendations_error")

    logger.info(
        "findings.built",
        dataset_id=str(report.dataset_id),
        total=len(all_findings),
        high=sum(1 for f in all_findings if f.severity.value == "HIGH"),
        medium=sum(1 for f in all_findings if f.severity.value == "MEDIUM"),
        low=sum(1 for f in all_findings if f.severity.value == "LOW"),
    )

    return FindingsReport(
        dataset_id=report.dataset_id,
        findings=all_findings,
    )
