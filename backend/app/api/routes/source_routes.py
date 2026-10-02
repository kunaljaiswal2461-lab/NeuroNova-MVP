"""JWT-only PostgreSQL source onboarding endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response

from app.core.config import get_settings
from app.core.dependencies import DBSession, SettingsDep, get_current_user
from app.db.models.user import User
from app.middlewares.rate_limiter import limiter
from app.schemas.source_schemas import (
    CatalogAssetDetailResponse,
    CatalogAssetListResponse,
    CatalogAssetReferenceResponse,
    CatalogAssetSummaryResponse,
    CatalogAvailabilityResponse,
    CatalogColumnChangeResponse,
    CatalogColumnIdentityChangeResponse,
    CatalogColumnResponse,
    CatalogCommentChangeResponse,
    CatalogDeclaredKeyResponse,
    CatalogForeignKeyChangeResponse,
    CatalogForeignKeyColumnMappingResponse,
    CatalogForeignKeyResponse,
    CatalogKeyChangeResponse,
    CatalogStructuralDiffResponse,
    CatalogSyncRunResponse,
    CatalogTableChangeResponse,
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


def _catalog_asset_reference_response(reference) -> CatalogAssetReferenceResponse:
    return CatalogAssetReferenceResponse(
        asset_id=reference.asset_id,
        schema_name=reference.schema_name,
        table_name=reference.table_name,
    )


def _catalog_asset_summary_response(asset) -> CatalogAssetSummaryResponse:
    return CatalogAssetSummaryResponse(
        asset_id=asset.asset_id,
        schema_name=asset.schema_name,
        table_name=asset.table_name,
        table_comment=asset.table_comment,
    )


def _catalog_column_response(column) -> CatalogColumnResponse:
    return CatalogColumnResponse(
        column_name=column.column_name,
        ordinal_position=column.ordinal_position,
        is_nullable=column.is_nullable,
        formatted_native_type=column.formatted_native_type,
        native_type_schema=column.native_type_schema,
        native_type_name=column.native_type_name,
        native_type_oid=column.native_type_oid,
        native_type_modifier=column.native_type_modifier,
        column_comment=column.column_comment,
    )


def _catalog_foreign_key_response(foreign_key) -> CatalogForeignKeyResponse:
    return CatalogForeignKeyResponse(
        constraint_name=foreign_key.constraint_name,
        target_status=foreign_key.target_status,
        referenced_asset=(
            _catalog_asset_reference_response(foreign_key.referenced_asset)
            if foreign_key.referenced_asset is not None
            else None
        ),
        column_mappings=[
            CatalogForeignKeyColumnMappingResponse(
                ordinal_position=mapping.ordinal_position,
                referencing_column_name=mapping.referencing_column_name,
                referenced_column_name=mapping.referenced_column_name,
            )
            for mapping in foreign_key.column_mappings
        ],
    )


def _catalog_asset_detail_response(detail) -> CatalogAssetDetailResponse:
    return CatalogAssetDetailResponse(
        source_id=detail.source_id,
        snapshot_id=detail.snapshot.id,
        snapshot_published_at=detail.snapshot.published_at,
        scope_matches_current_selection=detail.scope_matches_current_selection,
        asset=_catalog_asset_summary_response(detail.asset),
        columns=[_catalog_column_response(column) for column in detail.columns],
        declared_keys=[
            CatalogDeclaredKeyResponse(
                constraint_name=key.constraint_name,
                key_kind=key.key_kind,
                column_names=list(key.column_names),
            )
            for key in detail.declared_keys
        ],
        foreign_keys=[_catalog_foreign_key_response(foreign_key) for foreign_key in detail.foreign_keys],
    )


def _catalog_diff_response(diff) -> CatalogStructuralDiffResponse:
    return CatalogStructuralDiffResponse(
        source_id=diff.source_id,
        snapshot_id=diff.snapshot.id,
        snapshot_published_at=diff.snapshot.published_at,
        state=diff.state,
        previous_snapshot_id=(diff.previous_snapshot.id if diff.previous_snapshot is not None else None),
        previous_snapshot_published_at=(
            diff.previous_snapshot.published_at if diff.previous_snapshot is not None else None
        ),
        truncated=diff.truncated,
        change_limit=diff.change_limit,
        tables_added=[
            CatalogTableChangeResponse(schema_name=item.schema_name, table_name=item.table_name)
            for item in diff.tables_added
        ],
        tables_removed=[
            CatalogTableChangeResponse(schema_name=item.schema_name, table_name=item.table_name)
            for item in diff.tables_removed
        ],
        columns_added=[
            CatalogColumnIdentityChangeResponse(
                schema_name=item.schema_name,
                table_name=item.table_name,
                column_name=item.column_name,
            )
            for item in diff.columns_added
        ],
        columns_removed=[
            CatalogColumnIdentityChangeResponse(
                schema_name=item.schema_name,
                table_name=item.table_name,
                column_name=item.column_name,
            )
            for item in diff.columns_removed
        ],
        columns_changed=[
            CatalogColumnChangeResponse(
                schema_name=item.schema_name,
                table_name=item.table_name,
                column_name=item.column_name,
                previous_formatted_native_type=item.previous_formatted_native_type,
                current_formatted_native_type=item.current_formatted_native_type,
                previous_is_nullable=item.previous_is_nullable,
                current_is_nullable=item.current_is_nullable,
            )
            for item in diff.columns_changed
        ],
        keys_changed=[
            CatalogKeyChangeResponse(
                change=item.change,
                schema_name=item.schema_name,
                table_name=item.table_name,
                constraint_name=item.constraint_name,
                key_kind=item.key_kind,
                column_names=list(item.column_names),
            )
            for item in diff.keys_changed
        ],
        foreign_keys_changed=[
            CatalogForeignKeyChangeResponse(
                change=item.change,
                schema_name=item.schema_name,
                table_name=item.table_name,
                constraint_name=item.constraint_name,
                target_status=item.target_status,
                referenced_asset=(
                    _catalog_asset_reference_response(item.referenced_asset)
                    if item.referenced_asset is not None
                    else None
                ),
                column_mappings=[
                    CatalogForeignKeyColumnMappingResponse(
                        ordinal_position=mapping.ordinal_position,
                        referencing_column_name=mapping.referencing_column_name,
                        referenced_column_name=mapping.referenced_column_name,
                    )
                    for mapping in item.column_mappings
                ],
            )
            for item in diff.foreign_keys_changed
        ],
        comments_changed=[
            CatalogCommentChangeResponse(
                subject=item.subject,
                schema_name=item.schema_name,
                table_name=item.table_name,
                column_name=item.column_name,
                previous_comment=item.previous_comment,
                current_comment=item.current_comment,
            )
            for item in diff.comments_changed
        ],
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


@router.get("/{source_id}/catalog/assets", response_model=CatalogAssetListResponse)
async def list_catalog_assets(
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=50, ge=1, le=100),
    search: str | None = Query(default=None, max_length=128),
) -> CatalogAssetListResponse:
    """Browse one pinned snapshot without exposing no-longer-selected assets."""

    asset_page = await catalog_service.list_catalog_assets_for_owner(
        session,
        source_id=source_id,
        snapshot_id=snapshot_id,
        owner_user_id=current_user.id,
        page=page,
        page_size=page_size,
        search=search,
    )
    return CatalogAssetListResponse(
        source_id=asset_page.source_id,
        snapshot_id=asset_page.snapshot.id,
        snapshot_published_at=asset_page.snapshot.published_at,
        scope_matches_current_selection=asset_page.scope_matches_current_selection,
        page=asset_page.page,
        page_size=asset_page.page_size,
        total=asset_page.total,
        items=[_catalog_asset_summary_response(asset) for asset in asset_page.assets],
    )


@router.get(
    "/{source_id}/catalog/assets/{asset_id}", response_model=CatalogAssetDetailResponse
)
async def get_catalog_asset_detail(
    source_id: uuid.UUID,
    asset_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> CatalogAssetDetailResponse:
    """Read a pinned structural asset after source/snapshot/scope validation."""

    detail = await catalog_service.get_catalog_asset_detail_for_owner(
        session,
        source_id=source_id,
        snapshot_id=snapshot_id,
        asset_id=asset_id,
        owner_user_id=current_user.id,
    )
    return _catalog_asset_detail_response(detail)


@router.get("/{source_id}/catalog/diff", response_model=CatalogStructuralDiffResponse)
async def get_catalog_structural_diff(
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> CatalogStructuralDiffResponse:
    """Return a deterministic, safe comparison for one pinned publication."""

    diff = await catalog_service.get_catalog_structural_diff_for_owner(
        session,
        source_id=source_id,
        snapshot_id=snapshot_id,
        owner_user_id=current_user.id,
    )
    return _catalog_diff_response(diff)


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
