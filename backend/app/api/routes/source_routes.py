"""JWT-only PostgreSQL source onboarding endpoints.

Keep endpoint annotations concrete in this module.  SlowAPI wraps the route
callables, and the FastAPI version pinned by the deployment image evaluates
postponed annotations in the wrapper module rather than this module.  That
would incorrectly expose JSON bodies and injected dependencies as query
parameters.
"""

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
    MetricAggregationResponse,
    MetricCurrentCatalogResponse,
    MetricDefinitionCreateRequest,
    MetricDefinitionListResponse,
    MetricDefinitionResponse,
    MetricDefinitionVersionCreateRequest,
    MetricDefinitionVersionResponse,
    EligibleDeclaredRelationshipListResponse,
    EligibleDeclaredRelationshipResponse,
    PostgresTestRequest,
    PostgresTestResponse,
    SavedSourceCredentialReplacementRequest,
    SavedSourceTestResponse,
    RelationshipColumnPairResponse,
    RelationshipCurrentCatalogResponse,
    RelationshipDefinitionCreateRequest,
    RelationshipDefinitionListResponse,
    RelationshipDefinitionResponse,
    RelationshipDefinitionVersionCreateRequest,
    RelationshipDefinitionVersionResponse,
    RelationshipTargetKeyResponse,
    SourceDetailResponse,
    SourceCatalogRefreshPolicyRequest,
    SourceCatalogRefreshPolicyResponse,
    SourceListResponse,
    SourceProfileColumnObservationResponse,
    SourceProfileFindingListResponse,
    SourceProfileFindingResponse,
    SourceProfileObservationListResponse,
    SourceProfileRunListResponse,
    SourceProfileRunRequest,
    SourceProfileRunResponse,
    SourceProfileTableObservationResponse,
    SampleCollectedColumn,
    SampleColumnName,
    SampleSkippedColumn,
    SampleValuesConfigurationRequest,
    SampleValuesResponse,
    SourceProfilingConfigurationRequest,
    SourceProfilingConfigurationResponse,
    SourceScopeUpdateRequest,
)
from app.services import (
    catalog_service,
    metric_service,
    relationship_service,
    source_profile_service,
    source_service,
)
from app.services.sample_values import store as sample_value_store


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


def _profile_run_response(run) -> SourceProfileRunResponse:
    return SourceProfileRunResponse(
        run_id=run.id,
        snapshot_id=run.snapshot_id,
        state=run.status,
        requested_at=run.requested_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        attempt_count=run.attempt_count,
        next_attempt_at=run.next_attempt_at,
        failure_code=run.failure_code,
        sampling_method=run.sampling_method,
        sample_percent=run.sample_percent,
        sample_limit=run.sample_limit,
        sample_seed=run.sample_seed,
        max_operations=run.max_operations,
        selected_asset_count=run.selected_asset_count,
        selected_column_count=run.selected_column_count,
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
        column_id=column.column_id,
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
        foreign_key_id=foreign_key.foreign_key_id,
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


def _metric_version_response(view) -> MetricDefinitionVersionResponse:
    return MetricDefinitionVersionResponse(
        metric_id=view.metric_id,
        version_number=view.version_number,
        name=view.name,
        description=view.description,
        snapshot_id=view.snapshot_id,
        asset_id=view.asset_id,
        aggregation=MetricAggregationResponse(
            operation=view.aggregation_operation,
            column_id=view.aggregation_column_id,
            column_name=view.aggregation_column_name,
        ),
        fixed_filters=list(view.fixed_filters),
        unit_label=view.unit_label,
        default_date_column_id=view.default_date_column_id,
        default_date_column_name=view.default_date_column_name,
        default_time_zone=view.default_time_zone,
        confirmed_by_user_id=view.confirmed_by_user_id,
        confirmed_at=view.confirmed_at,
    )


def _metric_response(view) -> MetricDefinitionResponse:
    return MetricDefinitionResponse(
        **_metric_version_response(view).model_dump(),
        state=view.state,
        needs_review_reason=view.needs_review_reason,
        retired_at=view.retired_at,
        current_catalog=(
            MetricCurrentCatalogResponse(
                snapshot_id=view.current_catalog.snapshot_id,
                asset_id=view.current_catalog.asset_id,
                schema_name=view.current_catalog.asset_schema_name,
                table_name=view.current_catalog.asset_table_name,
                aggregation_column_id=view.current_catalog.aggregation_column_id,
                aggregation_column_name=view.current_catalog.aggregation_column_name,
                fixed_filters=list(view.current_catalog.fixed_filters),
                default_date_column_id=view.current_catalog.default_date_column_id,
                default_date_column_name=view.current_catalog.default_date_column_name,
                default_time_zone=view.current_catalog.default_time_zone,
            )
            if view.current_catalog is not None
            else None
        ),
    )


def _relationship_column_pair_response(pair) -> RelationshipColumnPairResponse:
    return RelationshipColumnPairResponse(
        ordinal_position=pair.ordinal_position,
        base_column_id=pair.base_column_id,
        base_column_name=pair.base_column_name,
        lookup_column_id=pair.lookup_column_id,
        lookup_column_name=pair.lookup_column_name,
    )


def _relationship_target_key_response(key) -> RelationshipTargetKeyResponse:
    return RelationshipTargetKeyResponse(
        key_id=key.key_id,
        constraint_name=key.constraint_name,
        key_kind=key.key_kind,
        lookup_column_ids=list(key.lookup_column_ids),
        lookup_column_names=list(key.lookup_column_names),
    )


def _relationship_version_response(view) -> RelationshipDefinitionVersionResponse:
    return RelationshipDefinitionVersionResponse(
        relationship_id=view.relationship_id,
        version_number=view.version_number,
        snapshot_id=view.snapshot_id,
        foreign_key_id=view.foreign_key_id,
        base_asset_id=view.base_asset_id,
        base_schema_name=view.base_schema_name,
        base_table_name=view.base_table_name,
        lookup_asset_id=view.lookup_asset_id,
        lookup_schema_name=view.lookup_schema_name,
        lookup_table_name=view.lookup_table_name,
        join_type=view.join_type,
        column_pairs=[
            _relationship_column_pair_response(pair) for pair in view.column_pairs
        ],
        target_key_id=view.target_key_id,
        target_key_constraint_name=view.target_key_constraint_name,
        target_key_kind=view.target_key_kind,
        confirmed_by_user_id=view.confirmed_by_user_id,
        confirmed_at=view.confirmed_at,
    )


def _relationship_response(view) -> RelationshipDefinitionResponse:
    return RelationshipDefinitionResponse(
        **_relationship_version_response(view).model_dump(),
        state=view.state,
        needs_review_reason=view.needs_review_reason,
        retired_at=view.retired_at,
        current_catalog=(
            RelationshipCurrentCatalogResponse(
                snapshot_id=view.current_catalog.snapshot_id,
                foreign_key_id=view.current_catalog.foreign_key_id,
                base_asset_id=view.current_catalog.base_asset_id,
                base_schema_name=view.current_catalog.base_schema_name,
                base_table_name=view.current_catalog.base_table_name,
                lookup_asset_id=view.current_catalog.lookup_asset_id,
                lookup_schema_name=view.current_catalog.lookup_schema_name,
                lookup_table_name=view.current_catalog.lookup_table_name,
                join_type=view.current_catalog.join_type,
                column_pairs=[
                    _relationship_column_pair_response(pair)
                    for pair in view.current_catalog.column_pairs
                ],
                target_key=_relationship_target_key_response(view.current_catalog.target_key),
            )
            if view.current_catalog is not None
            else None
        ),
    )


def _eligible_relationship_response(item) -> EligibleDeclaredRelationshipResponse:
    return EligibleDeclaredRelationshipResponse(
        snapshot_id=item.snapshot_id,
        foreign_key_id=item.foreign_key_id,
        constraint_name=item.constraint_name,
        base_asset_id=item.base_asset_id,
        base_schema_name=item.base_schema_name,
        base_table_name=item.base_table_name,
        lookup_asset_id=item.lookup_asset_id,
        lookup_schema_name=item.lookup_schema_name,
        lookup_table_name=item.lookup_table_name,
        column_pairs=[_relationship_column_pair_response(pair) for pair in item.column_pairs],
        target_key=_relationship_target_key_response(item.target_key),
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


@router.post("/{source_id}/retest", response_model=SavedSourceTestResponse)
@limiter.limit(get_settings().rate_limit_connector_test)
async def retest_saved_source(
    source_id: uuid.UUID,
    request: Request,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> SavedSourceTestResponse:
    """Retest a saved immutable endpoint using its protected credential."""

    return await source_service.retest_saved_postgresql_source_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        settings=settings,
    )


@router.put("/{source_id}/credentials", response_model=SourceDetailResponse)
@limiter.limit(get_settings().rate_limit_connector_create)
async def replace_saved_source_credentials(
    source_id: uuid.UUID,
    body: SavedSourceCredentialReplacementRequest,
    request: Request,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> SourceDetailResponse:
    """Validate and replace credentials without changing source identity."""

    return await source_service.replace_saved_postgresql_credentials_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        request=body,
        settings=settings,
    )


@router.put("/{source_id}/scope", response_model=SourceDetailResponse)
@limiter.limit(get_settings().rate_limit_connector_create)
async def update_saved_source_scope(
    source_id: uuid.UUID,
    body: SourceScopeUpdateRequest,
    request: Request,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> SourceDetailResponse:
    """Persist a revalidated explicit selected-table scope."""

    return await source_service.update_saved_postgresql_scope_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        request=body,
        settings=settings,
    )


@router.put("/{source_id}/catalog-refresh", response_model=SourceCatalogRefreshPolicyResponse)
@limiter.limit(get_settings().rate_limit_catalog_sync_request)
async def configure_saved_source_catalog_refresh(
    source_id: uuid.UUID,
    body: SourceCatalogRefreshPolicyRequest,
    request: Request,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SourceCatalogRefreshPolicyResponse:
    """Configure optional durable metadata maintenance; no inline discovery."""

    return await source_service.configure_saved_source_catalog_refresh_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        request=body,
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


# ── optional bounded database profiling (Stage 6A) ────────────────────────


@router.put(
    "/{source_id}/profiling",
    response_model=SourceProfilingConfigurationResponse,
)
@limiter.limit(get_settings().rate_limit_source_profile_request)
async def configure_source_profiling(
    source_id: uuid.UUID,
    body: SourceProfilingConfigurationRequest,
    request: Request,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SourceProfilingConfigurationResponse:
    """Explicit opt-in/out only; this endpoint never starts profile work."""

    source = await source_profile_service.configure_source_profiling_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        profiling_enabled=body.profiling_enabled,
    )
    return SourceProfilingConfigurationResponse(
        source_id=source.id,
        profiling_enabled=source.profiling_enabled,
        profiling_generation=source.profiling_generation,
    )


# ── sample values for text columns (SPEC-07) ──────────────────────────────


def _sample_values_response(state: sample_value_store.SampleValuesState) -> SampleValuesResponse:
    return SampleValuesResponse(
        enabled=state.enabled,
        sample_values_generation=state.generation,
        excluded_columns=[SampleColumnName(schema=s, table=t, column=c) for s, t, c in state.excluded_columns],
        collected=[
            SampleCollectedColumn(schema=s, table=t, column=c, method=m, value_count=n)
            for s, t, c, m, n in state.collected
        ],
        skipped=[SampleSkippedColumn(schema=s, table=t, column=c, reason=r) for s, t, c, r in state.skipped],
    )


@router.get("/{source_id}/sample-values", response_model=SampleValuesResponse)
async def get_source_sample_values(
    source_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SampleValuesResponse:
    """Switch, exclusions, and per-column counts for the head snapshot. Never returns a value."""

    state = await sample_value_store.get_sample_values_state(
        session, source_id=source_id, owner_user_id=current_user.id
    )
    return _sample_values_response(state)


@router.put("/{source_id}/sample-values", response_model=SampleValuesResponse)
@limiter.limit(get_settings().rate_limit_source_profile_request)
async def configure_source_sample_values(
    source_id: uuid.UUID,
    body: SampleValuesConfigurationRequest,
    request: Request,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SampleValuesResponse:
    """Owner-only switch and exclusions. Never contacts the source; new values appear at the next sync."""

    await sample_value_store.configure_sample_values_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        enabled=body.enabled,
        excluded_columns=(
            None
            if body.excluded_columns is None
            else [(c.schema_name, c.table, c.column) for c in body.excluded_columns]
        ),
    )
    state = await sample_value_store.get_sample_values_state(
        session, source_id=source_id, owner_user_id=current_user.id
    )
    return _sample_values_response(state)


@router.post(
    "/{source_id}/profile-runs",
    response_model=SourceProfileRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(get_settings().rate_limit_source_profile_request)
async def request_source_profile_run(
    source_id: uuid.UUID,
    body: SourceProfileRunRequest,
    request: Request,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> SourceProfileRunResponse:
    """Persist a manual bounded profile request, without remote I/O in web."""

    source_service.require_credential_operations(settings)
    run = await source_profile_service.request_source_profile_run_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        selections=tuple(
            source_profile_service.ProfileSelection(
                asset_id=table.asset_id,
                column_ids=tuple(table.column_ids),
            )
            for table in body.tables
        ),
        settings=settings,
    )
    return _profile_run_response(run)


@router.get("/{source_id}/profile-runs", response_model=SourceProfileRunListResponse)
async def list_source_profile_runs(
    source_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> SourceProfileRunListResponse:
    profile_page = await source_profile_service.list_source_profile_runs_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    return SourceProfileRunListResponse(
        source_id=profile_page.source_id,
        page=profile_page.page,
        page_size=profile_page.page_size,
        total=profile_page.total,
        items=[_profile_run_response(run) for run in profile_page.runs],
    )


@router.get("/{source_id}/profile-runs/{run_id}", response_model=SourceProfileRunResponse)
async def get_source_profile_run(
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SourceProfileRunResponse:
    run = await source_profile_service.get_source_profile_run_for_owner(
        session,
        source_id=source_id,
        run_id=run_id,
        owner_user_id=current_user.id,
    )
    return _profile_run_response(run)


@router.post(
    "/{source_id}/profile-runs/{run_id}/cancel",
    response_model=SourceProfileRunResponse,
)
async def cancel_source_profile_run(
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> SourceProfileRunResponse:
    run = await source_profile_service.cancel_source_profile_run_for_owner(
        session,
        source_id=source_id,
        run_id=run_id,
        owner_user_id=current_user.id,
    )
    return _profile_run_response(run)


@router.get(
    "/{source_id}/profile-runs/{run_id}/observations",
    response_model=SourceProfileObservationListResponse,
)
async def list_source_profile_observations(
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> SourceProfileObservationListResponse:
    profile_page = await source_profile_service.list_source_profile_observations_for_owner(
        session,
        source_id=source_id,
        run_id=run_id,
        owner_user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    return SourceProfileObservationListResponse(
        source_id=profile_page.source_id,
        run_id=profile_page.run.id,
        snapshot_id=profile_page.run.snapshot_id,
        page=profile_page.page,
        page_size=profile_page.page_size,
        total_tables=profile_page.total_tables,
        table_observations=[
            SourceProfileTableObservationResponse(
                observation_id=item.observation.id,
                asset_id=item.asset.id,
                schema_name=item.asset.schema_name,
                table_name=item.asset.table_name,
                collected_at=item.observation.collected_at,
                sampling_method=item.observation.sampling_method,
                sample_percent=item.observation.sample_percent,
                sample_limit=item.observation.sample_limit,
                sample_seed=item.observation.sample_seed,
                population=item.observation.population,
                observed_row_count=item.observation.observed_row_count,
                status=item.observation.status,
                warning_codes=list(item.observation.warning_codes),
            )
            for item in profile_page.table_observations
        ],
        column_observations=[
            SourceProfileColumnObservationResponse(
                observation_id=item.observation.id,
                table_observation_id=item.table_observation.id,
                asset_id=item.asset.id,
                column_id=item.column.id,
                column_name=item.column.column_name,
                formatted_native_type=item.column.formatted_native_type,
                scalar_kind=item.scalar_kind,
                collected_at=item.observation.collected_at,
                population=item.observation.population,
                observed_row_count=item.observation.observed_row_count,
                null_count=item.observation.null_count,
                non_null_count=item.observation.non_null_count,
                null_rate=item.observation.null_rate,
                distinct_count=item.observation.distinct_count,
                numeric_count=item.observation.numeric_count,
                numeric_sum=item.observation.numeric_sum,
                numeric_minimum=item.observation.numeric_minimum,
                numeric_maximum=item.observation.numeric_maximum,
                numeric_mean=item.observation.numeric_mean,
                status=item.observation.status,
                warning_codes=list(item.observation.warning_codes),
            )
            for item in profile_page.column_observations
        ],
    )


@router.get(
    "/{source_id}/profile-runs/{run_id}/findings",
    response_model=SourceProfileFindingListResponse,
)
async def list_source_profile_findings(
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=50, ge=1, le=100),
) -> SourceProfileFindingListResponse:
    profile_page = await source_profile_service.list_source_profile_findings_for_owner(
        session,
        source_id=source_id,
        run_id=run_id,
        owner_user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    return SourceProfileFindingListResponse(
        source_id=profile_page.source_id,
        run_id=profile_page.run.id,
        page=profile_page.page,
        page_size=profile_page.page_size,
        total=profile_page.total,
        items=[
            SourceProfileFindingResponse(
                finding_id=item.finding.id,
                table_observation_id=item.table_observation.id,
                column_observation_id=item.column_observation.id,
                asset_id=item.asset.id,
                schema_name=item.asset.schema_name,
                table_name=item.asset.table_name,
                column_id=item.column.id,
                column_name=item.column.column_name,
                finding_type=item.finding.finding_type,
                severity=item.finding.severity,
                title=item.finding.title,
                description=item.finding.description,
                limitations=item.finding.limitations,
                evidence=item.finding.evidence_json,
                generated_at=item.finding.generated_at,
            )
            for item in profile_page.findings
        ],
    )


# ── owner-confirmed single-table metrics ───────────────────────────────────

@router.post(
    "/{source_id}/metrics",
    response_model=MetricDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_metric_definition(
    source_id: uuid.UUID,
    body: MetricDefinitionCreateRequest,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> MetricDefinitionResponse:
    """Save an explicitly owner-confirmed, immutable metric version one."""

    created = await metric_service.create_metric_definition_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        request=body,
        settings=settings,
    )
    return _metric_response(created)


@router.get("/{source_id}/metrics", response_model=MetricDefinitionListResponse)
async def list_metric_definitions(
    source_id: uuid.UUID,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
    include_retired: bool = Query(default=True),
) -> MetricDefinitionListResponse:
    definitions = await metric_service.list_metric_definitions_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        include_retired=include_retired,
        settings=settings,
    )
    return MetricDefinitionListResponse(
        items=[_metric_response(item) for item in definitions], count=len(definitions)
    )


@router.get("/{source_id}/metrics/{metric_id}", response_model=MetricDefinitionResponse)
async def get_metric_definition(
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> MetricDefinitionResponse:
    definition = await metric_service.get_metric_definition_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=current_user.id,
        settings=settings,
    )
    return _metric_response(definition)


@router.post(
    "/{source_id}/metrics/{metric_id}/versions",
    response_model=MetricDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_metric_definition_version(
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    body: MetricDefinitionVersionCreateRequest,
    session: DBSession,
    settings: SettingsDep,
    current_user: User = Depends(get_current_user),
) -> MetricDefinitionResponse:
    """Edit by appending a fully re-confirmed immutable version."""

    created = await metric_service.create_metric_definition_version_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=current_user.id,
        request=body,
        settings=settings,
    )
    return _metric_response(created)


@router.get(
    "/{source_id}/metrics/{metric_id}/versions",
    response_model=MetricDefinitionListResponse,
)
async def list_metric_definition_versions(
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> MetricDefinitionListResponse:
    versions = await metric_service.list_metric_definition_versions_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=current_user.id,
    )
    return MetricDefinitionListResponse(
        items=[_metric_response(item) for item in versions], count=len(versions)
    )


@router.post(
    "/{source_id}/metrics/{metric_id}/retire",
    response_model=MetricDefinitionResponse,
)
async def retire_metric_definition(
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> MetricDefinitionResponse:
    """Retire a metric without erasing versions or historical provenance."""

    retired = await metric_service.retire_metric_definition_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=current_user.id,
    )
    return _metric_response(retired)


# ── owner-confirmed same-source relationships ─────────────────────────────

@router.post(
    "/{source_id}/relationships",
    response_model=RelationshipDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_relationship_definition(
    source_id: uuid.UUID,
    body: RelationshipDefinitionCreateRequest,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> RelationshipDefinitionResponse:
    """Save an explicit approval of one declared base-to-lookup FK direction."""

    created = await relationship_service.create_relationship_definition_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        request=body,
    )
    return _relationship_response(created)


@router.get(
    "/{source_id}/relationships/eligible",
    response_model=EligibleDeclaredRelationshipListResponse,
)
async def list_eligible_declared_relationships(
    source_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> EligibleDeclaredRelationshipListResponse:
    """List only current declared FKs that have matching unique-key evidence."""

    relationships = await relationship_service.list_eligible_declared_relationships_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
    )
    return EligibleDeclaredRelationshipListResponse(
        items=[_eligible_relationship_response(item) for item in relationships],
        count=len(relationships),
    )


@router.get(
    "/{source_id}/relationships",
    response_model=RelationshipDefinitionListResponse,
)
async def list_relationship_definitions(
    source_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
    include_retired: bool = Query(default=True),
) -> RelationshipDefinitionListResponse:
    definitions = await relationship_service.list_relationship_definitions_for_owner(
        session,
        source_id=source_id,
        owner_user_id=current_user.id,
        include_retired=include_retired,
    )
    return RelationshipDefinitionListResponse(
        items=[_relationship_response(item) for item in definitions],
        count=len(definitions),
    )


@router.get(
    "/{source_id}/relationships/{relationship_id}",
    response_model=RelationshipDefinitionResponse,
)
async def get_relationship_definition(
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> RelationshipDefinitionResponse:
    definition = await relationship_service.get_relationship_definition_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=current_user.id,
    )
    return _relationship_response(definition)


@router.post(
    "/{source_id}/relationships/{relationship_id}/versions",
    response_model=RelationshipDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_relationship_definition_version(
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    body: RelationshipDefinitionVersionCreateRequest,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> RelationshipDefinitionResponse:
    """Append a fully re-confirmed immutable relationship version."""

    created = await relationship_service.create_relationship_definition_version_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=current_user.id,
        request=body,
    )
    return _relationship_response(created)


@router.get(
    "/{source_id}/relationships/{relationship_id}/versions",
    response_model=RelationshipDefinitionListResponse,
)
async def list_relationship_definition_versions(
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> RelationshipDefinitionListResponse:
    versions = await relationship_service.list_relationship_definition_versions_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=current_user.id,
    )
    return RelationshipDefinitionListResponse(
        items=[_relationship_response(item) for item in versions],
        count=len(versions),
    )


@router.post(
    "/{source_id}/relationships/{relationship_id}/retire",
    response_model=RelationshipDefinitionResponse,
)
async def retire_relationship_definition(
    source_id: uuid.UUID,
    relationship_id: uuid.UUID,
    session: DBSession,
    current_user: User = Depends(get_current_user),
) -> RelationshipDefinitionResponse:
    """Retire an approval without deleting immutable history."""

    retired = await relationship_service.retire_relationship_definition_for_owner(
        session,
        source_id=source_id,
        relationship_id=relationship_id,
        owner_user_id=current_user.id,
    )
    return _relationship_response(retired)


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
