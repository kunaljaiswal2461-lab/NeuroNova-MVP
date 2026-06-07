"""Profiling + Findings + Viz + Insights + Indexing pipeline orchestrator.

End-to-end Layer 2 → Layer 3 → Layer 4 → Layer 5 → Layer 6 flow run as
a FastAPI BackgroundTask:
  1. mark PROFILING (5%)
  2. load raw file via Polars                  (→ 30%)
  3. run profiling engine                      (→ 60%)
  4. persist ProfileReport JSON to disk        (→ 65%)
  5. mark FINDINGS (68%), run findings layer   (→ 80%)
  6. persist FindingsReport JSON to disk       (→ 82%)
  7. mark VIZ (85%), run viz builder           (→ 93%)
  8. persist VizReport JSON to disk            (→ 95%)
  9. mark INSIGHTS (96%), run LLM layer        (→ 97%)
 10. persist InsightReport JSON to disk        (→ 97%)
 11. mark INDEXING (98%), embed + persist      (→ 99%)
 12. update row/col counts + COMPLETE (100%)
On any error → FAILED with error_message.
"""
from __future__ import annotations

import asyncio
import uuid

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.dataset import DatasetStatus
from app.db.session import get_sessionmaker
from app.services import dataset_service
from agentic_engine.profiler.engine import profile_dataset, save_report
from agentic_engine.findings.findings_builder import build_findings
from agentic_engine.findings.persistence import save_findings
from agentic_engine.viz.viz_builder import build_viz
from agentic_engine.viz.persistence import save_viz
from agentic_engine.llm_engine.insight_builder import build_insights
from agentic_engine.llm_engine.persistence import save_insights
from agentic_engine.vector_store.indexer import build_index


logger = get_logger("pipeline")


def _run_profile_sync(dataset_id, raw_path, file_type, settings):
    report = profile_dataset(
        dataset_id=dataset_id,
        raw_path=raw_path,
        file_type=file_type,
        settings=settings,
    )
    save_report(report, settings)
    return report


def _run_findings_sync(report, settings):
    findings_report = build_findings(report)
    save_findings(findings_report, settings)
    return findings_report


def _run_viz_sync(profile, findings_report, settings):
    viz_report = build_viz(profile, findings_report)
    save_viz(viz_report, settings)
    return viz_report


async def _run_insights(profile, findings_report, settings):
    insight_report = await build_insights(
        profile,
        findings_report,
        settings=settings,
    )
    save_insights(insight_report, settings)
    return insight_report


async def run_profiling(dataset_id: uuid.UUID) -> None:
    """Background entrypoint. Owns its own DB session."""
    settings = get_settings()
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        try:
            record = await dataset_service.get_dataset(session, dataset_id)
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.PROFILING, progress_pct=5,
            )
            logger.info("pipeline.started", dataset_id=str(dataset_id))

            raw_path = settings.raw_dir / record.filename
            file_type = record.file_type

            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.PROFILING, progress_pct=30,
            )

            # Layer 2 — profile
            profile = await asyncio.to_thread(
                _run_profile_sync, dataset_id, raw_path, file_type, settings
            )
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.PROFILING, progress_pct=65,
            )

            # Layer 3 — findings
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.FINDINGS, progress_pct=68,
            )
            findings_report = await asyncio.to_thread(
                _run_findings_sync, profile, settings
            )
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.FINDINGS, progress_pct=82,
            )

            # Layer 4 — viz metadata
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.VIZ, progress_pct=85,
            )
            viz_report = await asyncio.to_thread(
                _run_viz_sync, profile, findings_report, settings
            )
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.VIZ, progress_pct=95,
            )

            # Layer 5 — LLM insights (async; uses network I/O directly).
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.INSIGHTS, progress_pct=96,
            )
            insight_report = await _run_insights(profile, findings_report, settings)
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.INSIGHTS, progress_pct=97,
            )

            # Layer 6 — Vector indexing of findings (uses the active DB
            # session; the indexer commits its own transaction).
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.INDEXING, progress_pct=98,
            )
            index_report = await build_index(
                findings_report,
                session=session,
                settings=settings,
            )
            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.INDEXING, progress_pct=99,
            )

            # Persist derived metadata back onto the dataset record.
            record = await dataset_service.get_dataset(session, dataset_id)
            record.row_count = profile.schema_.row_count
            record.col_count = profile.schema_.col_count
            await session.commit()

            await dataset_service.mark_status(
                session, dataset_id,
                status=DatasetStatus.COMPLETE, progress_pct=100,
            )
            logger.info(
                "pipeline.complete",
                dataset_id=str(dataset_id),
                rows=profile.schema_.row_count,
                cols=profile.schema_.col_count,
                health=profile.health.score,
                grade=profile.health.grade,
                findings=findings_report.count,
                charts=viz_report.count,
                insights_explanations=insight_report.explanation_count,
                insights_questions=insight_report.question_count,
                insights_degraded=insight_report.degraded,
                indexed=index_report.indexed_count,
                indexing_degraded=index_report.degraded,
            )
        except Exception as exc:
            logger.exception(
                "pipeline.failed",
                dataset_id=str(dataset_id),
                error=str(exc),
            )
            try:
                await dataset_service.mark_status(
                    session, dataset_id,
                    status=DatasetStatus.FAILED,
                    error_message=str(exc),
                )
            except Exception:
                logger.exception("pipeline.status_update_failed")
