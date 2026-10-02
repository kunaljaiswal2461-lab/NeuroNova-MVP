"""JWT-only PostgreSQL source onboarding endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response

from app.core.config import get_settings
from app.core.dependencies import DBSession, SettingsDep, get_current_user
from app.db.models.user import User
from app.middlewares.rate_limiter import limiter
from app.schemas.source_schemas import (
    CatalogAvailabilityResponse,
    CatalogSyncRunResponse,
    CreateSourceRequest,
    PostgresTestRequest,
    PostgresTestResponse,
    SourceDetailResponse,
    SourceListResponse,
)
from app.services import catalog_service, source_service


router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


def _catalog_run_response(run) -> CatalogSyncRunResponse:
    return CatalogSyncRunResponse(
        run_id=run.id,
        state=run.status,
        requested_at=run.requested_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        attempt_count=run.attempt_count,
        next_attempt_at=run.next_attempt_at,
        failure_code=run.failure_code,
    )


@router.post("/postgresql/test", response_model=PostgresTestResponse)
@limiter.limit(get_settings().rate_limit_connector_test)
async def test_postgresql_connection(
    request: Request,
    body: PostgresTestRequest,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> PostgresTestResponse:
    """Test transient credentials and return a bounded names-only inventory."""
    # ``current_user`` is intentionally unused after dependency resolution:
    # merely reaching the remote adapter requires a trusted JWT identity.
    del current_user
    return await source_service.test_postgresql_source(body, settings=settings)


@router.post("", response_model=SourceDetailResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_settings().rate_limit_connector_create)
async def create_source(
    request: Request,
    body: CreateSourceRequest,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> SourceDetailResponse:
    return await source_service.create_postgresql_source(
        session,
        owner_user_id=current_user.id,
        request=body,
        settings=settings,
    )


@router.post(
    "/{source_id}/catalog-syncs",
    response_model=CatalogSyncRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(get_settings().rate_limit_catalog_sync_request)
async def request_catalog_sync(
    source_id: uuid.UUID,
    request: Request,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> CatalogSyncRunResponse:
    """Persist/coalesce one owner-scoped manual metadata-refresh request."""

    # Do not create work that no correctly configured worker may execute.
    # This shared Stage 2 activation boundary also validates encryption-key
    # availability without decrypting the source credential in the web path.
    source_service.require_credential_operations(settings)
    run = await catalog_service.request_catalog_sync_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
    )
    return _catalog_run_response(run)


@router.get("/{source_id}/catalog", response_model=CatalogAvailabilityResponse)
async def get_catalog_availability(
    source_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> CatalogAvailabilityResponse:
    availability = await catalog_service.get_catalog_availability_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
    )
    latest_run = availability.latest_run
    snapshot = availability.current_snapshot
    return CatalogAvailabilityResponse(
        source_id=source_id,
        latest_run=_catalog_run_response(latest_run) if latest_run is not None else None,
        current_snapshot_id=snapshot.id if snapshot is not None else None,
        current_snapshot_published_at=snapshot.published_at if snapshot is not None else None,
        older_snapshot_available=(
            snapshot is not None
            and (latest_run is None or latest_run.status.value != "SUCCEEDED" or snapshot.sync_run_id != latest_run.id)
        ),
    )


@router.get("/{source_id}/catalog-syncs/{run_id}", response_model=CatalogSyncRunResponse)
async def get_catalog_sync_run(
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> CatalogSyncRunResponse:
    run = await catalog_service.get_catalog_sync_run_for_owner(
        session,
        source_id=source_id,
        run_id=run_id,
        owner_user_id=current_user.id,
    )
    return _catalog_run_response(run)


@router.post(
    "/{source_id}/catalog-syncs/{run_id}/cancel",
    response_model=CatalogSyncRunResponse,
)
async def cancel_catalog_sync(
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> CatalogSyncRunResponse:
    run = await catalog_service.cancel_catalog_sync_run_for_owner(
        session,
        source_id=source_id,
        run_id=run_id,
        owner_user_id=current_user.id,
    )
    return _catalog_run_response(run)


@router.get("", response_model=SourceListResponse)
async def list_sources(
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SourceListResponse:
    return await source_service.list_sources_for_owner(session, owner_user_id=current_user.id)


@router.get("/{source_id}", response_model=SourceDetailResponse)
async def get_source(
    source_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SourceDetailResponse:
    return await source_service.read_source_for_owner(
        session, source_id=source_id, owner_user_id=current_user.id
    )


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_source(
    source_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> Response:
    await source_service.delete_source_for_owner(
        session, source_id=source_id, owner_user_id=current_user.id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
