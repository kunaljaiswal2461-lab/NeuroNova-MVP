from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile, status

from app.core.dependencies import AuthContext, DBSession, SettingsDep, StorageDep, get_auth_context
from app.db.models.dataset import DatasetStatus
from app.exceptions.custom_exceptions import NotFoundError
from app.schemas.response_schemas import (
    DatasetList,
    DatasetStatusResponse,
    DatasetSummary,
    UploadAck,
)
from app.services import dataset_service
from agentic_engine.profiler.engine import load_report
from agentic_engine.findings.persistence import load_findings_raw
from agentic_engine.viz.persistence import load_viz_raw
from agentic_engine.llm_engine.persistence import load_insights_raw
from agentic_engine.vector_store.models import RetrievalQuery, RetrievalResult
from agentic_engine.vector_store.retriever import retrieve as run_retrieve
from agentic_engine.workflows.profiling_pipeline import run_profiling


router = APIRouter(
    prefix="/api/v1",
    tags=["datasets"],
    dependencies=[AuthContext],  # accepts JWT Bearer or legacy X-API-Key
)


@router.post(
    "/upload",
    response_model=UploadAck,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a dataset",
)
async def upload_dataset(
    background_tasks: BackgroundTasks,
    session: DBSession,
    storage: StorageDep,
    settings: SettingsDep,
    file: UploadFile = File(..., description="CSV / XLSX / JSON / Parquet"),
    current_user=Depends(get_auth_context),
) -> UploadAck:
    record = await dataset_service.create_dataset_from_upload(
        upload=file,
        session=session,
        storage=storage,
        settings=settings,
    )
    # Stamp the uploading user so we can trace which user owns which dataset
    if current_user is not None:
        record.user_id = current_user.id
        await session.commit()
    background_tasks.add_task(run_profiling, record.id)
    return UploadAck.model_validate(record)


@router.get(
    "/datasets/{dataset_id}/status",
    response_model=DatasetStatusResponse,
    summary="Poll profiling status for a dataset",
)
async def get_dataset_status(
    dataset_id: uuid.UUID,
    session: DBSession,
) -> DatasetStatusResponse:
    record = await dataset_service.get_dataset(session, dataset_id)
    return DatasetStatusResponse.model_validate(record)


@router.get(
    "/datasets",
    response_model=DatasetList,
    summary="List datasets (most recent first)",
)
async def list_datasets(
    session: DBSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user=Depends(get_auth_context),
) -> DatasetList:
    user_id = current_user.id if current_user is not None else None
    items, total = await dataset_service.list_datasets(
        session, limit=limit, offset=offset, user_id=user_id
    )
    return DatasetList(
        items=[DatasetSummary.model_validate(it) for it in items],
        count=total,
    )


@router.get(
    "/datasets/{dataset_id}",
    response_model=DatasetSummary,
    summary="Fetch dataset metadata",
)
async def get_dataset(
    dataset_id: uuid.UUID,
    session: DBSession,
) -> DatasetSummary:
    record = await dataset_service.get_dataset(session, dataset_id)
    return DatasetSummary.model_validate(record)


@router.get(
    "/datasets/{dataset_id}/profile",
    summary="Fetch the ProfileReport JSON for a profiled dataset",
)
async def get_dataset_profile(
    dataset_id: uuid.UUID,
    session: DBSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    record = await dataset_service.get_dataset(session, dataset_id)
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"profile for dataset {dataset_id} is not ready",
            details={"status": record.status.value},
        )
    report = load_report(dataset_id, settings)
    if report is None:
        raise NotFoundError(
            f"profile artifact missing for dataset {dataset_id}",
            details={"dataset_id": str(dataset_id)},
        )
    return report


@router.get(
    "/datasets/{dataset_id}/findings",
    summary="Fetch the FindingsReport for a profiled dataset",
)
async def get_dataset_findings(
    dataset_id: uuid.UUID,
    session: DBSession,
    settings: SettingsDep,
    severity: str | None = Query(
        None,
        description="Filter by severity: HIGH, MEDIUM, or LOW",
    ),
    type: str | None = Query(
        None,
        description="Filter by FindingType (e.g. HIGH_NULLABILITY)",
    ),
    column: str | None = Query(
        None,
        description="Filter findings for a specific column name",
    ),
) -> dict:
    record = await dataset_service.get_dataset(session, dataset_id)
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"findings for dataset {dataset_id} are not ready",
            details={"status": record.status.value},
        )
    raw = load_findings_raw(dataset_id, settings)
    if raw is None:
        raise NotFoundError(
            f"findings artifact missing for dataset {dataset_id}",
            details={"dataset_id": str(dataset_id)},
        )

    findings = raw.get("findings", [])

    if severity:
        sev_upper = severity.upper()
        findings = [f for f in findings if f.get("severity") == sev_upper]
    if type:
        type_upper = type.upper()
        findings = [f for f in findings if f.get("type") == type_upper]
    if column:
        findings = [f for f in findings if f.get("column") == column]

    return {
        "dataset_id": raw["dataset_id"],
        "generated_at": raw["generated_at"],
        "total": len(findings),
        "filters": {"severity": severity, "type": type, "column": column},
        "findings": findings,
    }


@router.get(
    "/datasets/{dataset_id}/viz",
    summary="Fetch the VizReport (chart specifications) for a profiled dataset",
)
async def get_dataset_viz(
    dataset_id: uuid.UUID,
    session: DBSession,
    settings: SettingsDep,
    type: str | None = Query(
        None,
        description="Filter by ChartType (HISTOGRAM, BAR, SCATTER, HEATMAP, BOXPLOT, TIMESERIES, PIE)",
    ),
    column: str | None = Query(
        None,
        description="Return only charts that involve this column",
    ),
) -> dict:
    record = await dataset_service.get_dataset(session, dataset_id)
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"viz for dataset {dataset_id} is not ready",
            details={"status": record.status.value},
        )
    raw = load_viz_raw(dataset_id, settings)
    if raw is None:
        raise NotFoundError(
            f"viz artifact missing for dataset {dataset_id}",
            details={"dataset_id": str(dataset_id)},
        )

    charts = raw.get("charts", [])

    if type:
        type_upper = type.upper()
        charts = [c for c in charts if c.get("type") == type_upper]
    if column:
        charts = [c for c in charts if column in c.get("columns", [])]

    return {
        "dataset_id": raw["dataset_id"],
        "generated_at": raw["generated_at"],
        "total": len(charts),
        "filters": {"type": type, "column": column},
        "charts": charts,
        "skipped_columns": raw.get("skipped_columns", []),
        "total_columns": raw.get("total_columns", 0),
        "rendered_columns": raw.get("rendered_columns", 0),
    }


@router.get(
    "/datasets/{dataset_id}/insights",
    summary="Fetch the LLM InsightReport (summary + explanations + questions)",
)
async def get_dataset_insights(
    dataset_id: uuid.UUID,
    session: DBSession,
    settings: SettingsDep,
    section: str | None = Query(
        None,
        description=(
            "Return only one section: summary, explanations, or questions. "
            "Omit to return the full report."
        ),
    ),
) -> dict[str, Any]:
    record = await dataset_service.get_dataset(session, dataset_id)
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"insights for dataset {dataset_id} are not ready",
            details={"status": record.status.value},
        )
    raw = load_insights_raw(dataset_id, settings)
    if raw is None:
        raise NotFoundError(
            f"insights artifact missing for dataset {dataset_id}",
            details={"dataset_id": str(dataset_id)},
        )

    if section is None:
        return raw

    section_key = section.strip().lower()
    section_map = {
        "summary": "executive_summary",
        "explanations": "finding_explanations",
        "questions": "suggested_questions",
    }
    if section_key not in section_map:
        raise NotFoundError(
            f"unknown section '{section}' — choose summary, explanations, or questions",
            details={"supported": list(section_map)},
        )

    target = section_map[section_key]
    return {
        "dataset_id": raw["dataset_id"],
        "generated_at": raw["generated_at"],
        "section": section_key,
        target: raw.get(target),
    }


@router.post(
    "/datasets/{dataset_id}/retrieve",
    response_model=RetrievalResult,
    summary="Cosine-similarity search over a dataset's finding embeddings",
)
async def retrieve_findings(
    dataset_id: uuid.UUID,
    query: RetrievalQuery,
    session: DBSession,
    settings: SettingsDep,
) -> RetrievalResult:
    """RAG-style retrieval endpoint backing Layer 7's chat agent.

    Returns the top-K most semantically similar findings to the supplied
    query, optionally pre-filtered by severity, finding type, or column.
    Empty result with ``degraded=True`` if the dataset has no embeddings
    indexed yet (e.g. pipeline still running or OpenAI key absent).
    """
    record = await dataset_service.get_dataset(session, dataset_id)
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"dataset {dataset_id} is not ready for retrieval",
            details={"status": record.status.value},
        )
    return await run_retrieve(
        dataset_id,
        query,
        session=session,
        settings=settings,
    )
