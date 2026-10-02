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
    CreateSourceRequest,
    PostgresTestRequest,
    PostgresTestResponse,
    SourceDetailResponse,
    SourceListResponse,
)
from app.services import source_service


router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


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
