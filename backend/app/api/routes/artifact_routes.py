"""Authenticated compatibility routes for persisted dataset artifacts.

The active UI reads visualization specifications from ``/api/v1/datasets``.
This route preserves the older ``/viz/{dataset_id}.json`` URL without exposing
the entire on-disk artifact directory through a static mount.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.dependencies import AuthContext, DBSession, DatasetAccessDep, SettingsDep
from app.db.models.dataset import DatasetStatus
from app.exceptions.custom_exceptions import NotFoundError
from app.services import dataset_service
from agentic_engine.viz.persistence import viz_path


router = APIRouter(
    tags=["artifacts"],
    dependencies=[AuthContext],
)


@router.get("/viz/{dataset_id}.json", include_in_schema=False)
async def get_legacy_viz_artifact(
    dataset_id: uuid.UUID,
    session: DBSession,
    settings: SettingsDep,
    access: DatasetAccessDep,
) -> FileResponse:
    """Serve one persisted VizReport only after dataset authorization."""
    record = await dataset_service.get_dataset_for_access(
        session, dataset_id, access=access
    )
    if record.status is not DatasetStatus.COMPLETE:
        raise NotFoundError(
            f"viz for dataset {dataset_id} is not ready",
            details={"status": record.status.value},
        )

    path = viz_path(dataset_id, settings)
    if not path.is_file():
        raise NotFoundError(
            f"viz artifact missing for dataset {dataset_id}",
            details={"dataset_id": str(dataset_id)},
        )
    return FileResponse(path, media_type="application/json")
