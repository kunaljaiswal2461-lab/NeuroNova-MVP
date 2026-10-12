"""Owner-confirmed, single-table PostgreSQL metric definition boundary.

This module stores only a small immutable metric contract and deterministically
rematerializes it against the current selected catalog before execution.  It
does not create SQL, infer business definitions, access credentials, or retain
result rows.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models.catalog import CatalogAsset, CatalogColumn
from app.db.models.metric_definition import (
    MetricAggregateOperation,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricLifecycleState,
)
from app.exceptions.custom_exceptions import AppError, NotFoundError
from app.schemas.source_schemas import MetricDefinitionWriteRequest
from app.services.catalog_service import (
    CatalogPersistenceError,
    CatalogQueryContextError,
    get_postgres_chat_asset_columns_for_owner,
    get_postgres_chat_catalog_context_for_owner,
)
from app.services.postgres_query_compiler import (
    PostgresQueryPlanValidationError,
    validate_postgres_query_plan,
)
from app.services.postgres_query_executor import PostgresMetricProvenance
from app.services.postgres_query_plan import (
    PostgresAggregation,
    PostgresApprovedJoin,
    PostgresFilter,
    PostgresGroupDimension,
    PostgresQueryPlan,
    PostgresSort,
)
from app.services.source_service import get_source_for_owner


class MetricServiceError(AppError):
    pass


class MetricValidationError(MetricServiceError):
    status_code = 422
    code = "invalid_metric_definition"

    def __init__(self, message: str = "metric definition is invalid"):
        super().__init__(message)


class MetricConflictError(MetricServiceError):
    status_code = 409
    code = "metric_definition_conflict"

    def __init__(self, message: str = "metric definition conflicts with current source state"):
        super().__init__(message)


class MetricNeedsReviewError(MetricServiceError):
    status_code = 409
    code = "metric_definition_needs_review"

    def __init__(self, message: str = "metric definition needs owner review before it can be used"):
        super().__init__(message)


class MetricRetiredError(MetricServiceError):
    status_code = 409
    code = "metric_definition_retired"

    def __init__(self):
        super().__init__("metric definition is retired and cannot be used")


class MetricCatalogUnavailableError(MetricServiceError):
    status_code = 409
    code = "metric_catalog_unavailable"

    def __init__(self):
        super().__init__("the current authorized catalog is unavailable for this metric")


class MetricPersistenceError(MetricServiceError):
    status_code = 503
    code = "metric_definition_persistence_failed"

    def __init__(self):
        super().__init__("could not complete the metric definition request")


class _MetricRequirementError(Exception):
    """A current catalog no longer satisfies an immutable structural contract."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class MetricMatch:
    kind: Literal["matched", "ambiguous", "unavailable"]
    metric_id: uuid.UUID | None = None
    version_number: int | None = None
    message: str | None = None


@dataclass(frozen=True)
class ResolvedMetricDefinition:
    """One current, executable owner-confirmed metric rematerialized safely."""

    metric_id: uuid.UUID
    metric_version_id: uuid.UUID
    version_number: int
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    asset_schema_name: str
    asset_table_name: str
    display_name: str
    description: str | None
    aggregation: Literal["count_all", "count_column", "sum", "average", "minimum", "maximum"]
    aggregation_column_id: uuid.UUID | None
    aggregation_column_name: str | None
    fixed_filters: tuple[PostgresFilter, ...]
    unit_label: str | None
    default_date_column_id: uuid.UUID | None
    default_date_column_name: str | None
    default_time_zone: str | None
    confirmed_at: datetime
    confirmed_by_user_id: uuid.UUID
    allowed_column_ids: frozenset[uuid.UUID]


@dataclass(frozen=True)
class MetricCurrentCatalogView:
    """Live references for editing a still-valid immutable version.

    The version's confirmed snapshot/asset fields remain historical audit
    facts.  This separate projection lets an owner re-confirm a compatible
    definition after catalog IDs rotate on a new snapshot, without treating
    stale UUIDs as current authority.
    """

    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    asset_schema_name: str
    asset_table_name: str
    aggregation_column_id: uuid.UUID | None
    aggregation_column_name: str | None
    fixed_filters: tuple[PostgresFilter, ...]
    default_date_column_id: uuid.UUID | None
    default_date_column_name: str | None
    default_time_zone: str | None


@dataclass(frozen=True)
class MetricDefinitionView:
    metric_id: uuid.UUID
    version_number: int
    name: str
    description: str | None
    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    aggregation_operation: Literal["count", "sum", "average", "minimum", "maximum"]
    aggregation_column_id: uuid.UUID | None
    aggregation_column_name: str | None
    fixed_filters: tuple[PostgresFilter, ...]
    unit_label: str | None
    default_date_column_id: uuid.UUID | None
    default_date_column_name: str | None
    default_time_zone: str | None
    confirmed_by_user_id: uuid.UUID
    confirmed_at: datetime
    state: Literal["valid", "needs_review", "retired"]
    needs_review_reason: str | None
    retired_at: datetime | None
    current_catalog: MetricCurrentCatalogView | None = None


_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_STRUCTURAL_KEYS = (
    "column_name",
    "native_type_schema",
    "native_type_name",
    "native_type_oid",
    "native_type_modifier",
    "is_nullable",
)
_MAX_METRIC_MATCHES = 100


def normalize_metric_name(value: str) -> str:
    """Use NFKC + trim/collapsed whitespace + Unicode casefold for matching.

    Punctuation is deliberately preserved: stripping it could silently merge
    two owner-facing display names.  The original spelling is always retained
    in ``display_name`` and version history.
    """

    if not isinstance(value, str) or "\x00" in value:
        raise MetricValidationError("metric name is invalid")
    normalized = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold()
    if not normalized or len(normalized) > 128:
        raise MetricValidationError("metric name is invalid")
    return normalized


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _operation_from_request(operation: str, column_id: uuid.UUID | None) -> Literal[
    "count_all", "count_column", "sum", "average", "minimum", "maximum"
]:
    if operation == "count":
        return "count_column" if column_id is not None else "count_all"
    if operation in {"sum", "average", "minimum", "maximum"}:
        assert column_id is not None
        return operation
    raise MetricValidationError("metric aggregate operation is invalid")


def _operation_to_public(operation: MetricAggregateOperation) -> Literal[
    "count", "sum", "average", "minimum", "maximum"
]:
    return {
        MetricAggregateOperation.COUNT: "count",
        MetricAggregateOperation.SUM: "sum",
        MetricAggregateOperation.AVERAGE: "average",
        MetricAggregateOperation.MINIMUM: "minimum",
        MetricAggregateOperation.MAXIMUM: "maximum",
    }[operation]


def _metric_enum(operation: str) -> MetricAggregateOperation:
    try:
        return MetricAggregateOperation(operation.upper())
    except ValueError as exc:
        raise MetricValidationError("metric aggregate operation is invalid") from exc


def _column_contract(column: CatalogColumn) -> dict[str, Any]:
    return {
        "column_id": str(column.id),
        "column_name": column.column_name,
        "native_type_schema": column.native_type_schema,
        "native_type_name": column.native_type_name,
        "native_type_oid": column.native_type_oid,
        "native_type_modifier": column.native_type_modifier,
        "is_nullable": column.is_nullable,
    }


def _filter_payload(filter_item: PostgresFilter, column: CatalogColumn) -> dict[str, Any]:
    payload = filter_item.model_dump(mode="json")
    payload.pop("column_id", None)
    return {"column": _column_contract(column), "filter": payload}


def _column_from_contract(
    contract: object,
    *,
    columns_by_name: dict[str, CatalogColumn],
) -> CatalogColumn:
    if not isinstance(contract, dict):
        raise _MetricRequirementError("definition_invalid")
    name = contract.get("column_name")
    if not isinstance(name, str):
        raise _MetricRequirementError("definition_invalid")
    column = columns_by_name.get(name)
    if column is None:
        raise _MetricRequirementError("column_missing")
    for key in _STRUCTURAL_KEYS:
        if contract.get(key) != getattr(column, key):
            raise _MetricRequirementError("column_changed")
    return column


async def _current_asset_and_columns(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    requested_snapshot_id: uuid.UUID | None = None,
    requested_asset_id: uuid.UUID | None = None,
) -> tuple[Any, CatalogAsset | None, tuple[CatalogColumn, ...]]:
    try:
        context = await get_postgres_chat_catalog_context_for_owner(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        if requested_snapshot_id is not None and context.snapshot.id != requested_snapshot_id:
            raise MetricConflictError("catalog snapshot changed; reload the selected table before confirming")
        # A caller which only needs the current owner-scoped context must not
        # be forced to invent an asset ID.  In particular, rematerialization
        # resolves the immutable schema/table identity first, then asks for
        # that current asset's columns.  Confirmation callers always supply
        # an asset ID and retain the strict selected-scope check below.
        if requested_asset_id is None:
            return context, None, ()
        asset = next((item for item in context.assets if item.id == requested_asset_id), None)
        if asset is None:
            raise MetricValidationError("metric asset is not in the current selected source scope")
        columns = await get_postgres_chat_asset_columns_for_owner(
            session,
            source_id=source_id,
            snapshot_id=context.snapshot.id,
            asset_id=asset.id,
            owner_user_id=owner_user_id,
        )
        return context, asset, columns
    except MetricServiceError:
        raise
    except (CatalogQueryContextError, CatalogPersistenceError):
        raise MetricCatalogUnavailableError() from None
    except NotFoundError:
        # Retain the source route's non-enumerating ownership behavior.
        raise NotFoundError("source not found") from None


def _base_plan(
    *,
    asset_id: uuid.UUID,
    operation: Literal["count_all", "count_column", "sum", "average", "minimum", "maximum"],
    aggregation_column_id: uuid.UUID | None,
    filters: tuple[PostgresFilter, ...],
    default_date_column_id: uuid.UUID | None = None,
    default_time_zone: str | None = None,
) -> PostgresQueryPlan:
    aggregation = PostgresAggregation(operation=operation, column_id=aggregation_column_id)
    dimensions: tuple[PostgresGroupDimension, ...] = ()
    if default_date_column_id is not None:
        dimensions = (
            PostgresGroupDimension(
                column_id=default_date_column_id,
                bucket="day",
                time_zone=default_time_zone,
            ),
        )
    return PostgresQueryPlan(
        asset_id=asset_id,
        aggregations=(aggregation,),
        filters=filters,
        group_by=dimensions,
        limit=1,
    )


async def _validate_confirmation_request(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: MetricDefinitionWriteRequest,
    settings: Settings,
) -> tuple[Any, CatalogAsset, tuple[CatalogColumn, ...], dict[str, Any]]:
    if request.owner_confirmed is not True:
        raise MetricValidationError("owner confirmation is required")
    context, asset, columns = await _current_asset_and_columns(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
        requested_snapshot_id=request.snapshot_id,
        requested_asset_id=request.asset_id,
    )
    # ``request.asset_id`` is mandatory, so the context-only branch above is
    # not reachable here.  Keep this guard so a future schema relaxation
    # cannot accidentally create a metric without a selected table.
    if asset is None:
        raise MetricValidationError("metric asset is required")
    columns_by_id = {column.id: column for column in columns}
    aggregation_column = (
        columns_by_id.get(request.aggregation.column_id)
        if request.aggregation.column_id is not None
        else None
    )
    if request.aggregation.column_id is not None and aggregation_column is None:
        raise MetricValidationError("metric aggregate column is not in the selected table")
    operation = _operation_from_request(request.aggregation.operation, request.aggregation.column_id)
    for filter_item in request.fixed_filters:
        if filter_item.column_id not in columns_by_id:
            raise MetricValidationError("metric fixed filter column is not in the selected table")
    default_date_column = (
        columns_by_id.get(request.default_date_column_id)
        if request.default_date_column_id is not None
        else None
    )
    if request.default_date_column_id is not None and default_date_column is None:
        raise MetricValidationError("metric default date column is not in the selected table")
    try:
        # The existing compiler validator remains the definitive typed
        # aggregate/filter/time boundary. A default date is deliberately
        # checked as a valid day bucket only when one was confirmed.
        await validate_postgres_query_plan(
            session,
            owner_user_id=owner_user_id,
            source_id=source_id,
            snapshot_id=context.snapshot.id,
            plan=_base_plan(
                asset_id=asset.id,
                operation=operation,
                aggregation_column_id=(aggregation_column.id if aggregation_column else None),
                filters=tuple(request.fixed_filters),
                default_date_column_id=(default_date_column.id if default_date_column else None),
                default_time_zone=request.default_time_zone,
            ),
            settings=settings,
        )
    except (PostgresQueryPlanValidationError, ValidationError, ValueError) as exc:
        raise MetricValidationError("metric aggregate, filters, or time defaults are incompatible with the current catalog") from exc

    payload = {
        "schema_version": 1,
        "asset": {
            "schema_name": asset.schema_name,
            "table_name": asset.table_name,
        },
        "aggregation": {
            "operation": request.aggregation.operation,
            "column": _column_contract(aggregation_column) if aggregation_column is not None else None,
        },
        "fixed_filters": [
            _filter_payload(filter_item, columns_by_id[filter_item.column_id])
            for filter_item in request.fixed_filters
        ],
        "default_date_column": (
            _column_contract(default_date_column) if default_date_column is not None else None
        ),
    }
    return context, asset, columns, payload


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass


async def _definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    lock: bool = False,
) -> MetricDefinition:
    statement = select(MetricDefinition).where(
        MetricDefinition.id == metric_id,
        MetricDefinition.source_id == source_id,
        MetricDefinition.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update()
    try:
        definition = (await session.execute(statement)).scalar_one_or_none()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None
    if definition is None:
        await _rollback_quietly(session)
        raise NotFoundError("metric definition not found")
    return definition


async def _version_for_definition(
    session: AsyncSession,
    *,
    definition: MetricDefinition,
    version_number: int,
) -> MetricDefinitionVersion:
    try:
        version = (
            await session.execute(
                select(MetricDefinitionVersion).where(
                    MetricDefinitionVersion.source_id == definition.source_id,
                    MetricDefinitionVersion.metric_definition_id == definition.id,
                    MetricDefinitionVersion.version_number == version_number,
                )
            )
        ).scalar_one_or_none()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None
    if version is None:
        await _rollback_quietly(session)
        raise NotFoundError("metric definition version not found")
    return version


async def _active_name_conflict(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    normalized_name: str,
    except_metric_id: uuid.UUID | None = None,
) -> bool:
    statement = select(MetricDefinition.id).where(
        MetricDefinition.source_id == source_id,
        MetricDefinition.normalized_name == normalized_name,
        MetricDefinition.lifecycle_state == MetricLifecycleState.ACTIVE,
    )
    if except_metric_id is not None:
        statement = statement.where(MetricDefinition.id != except_metric_id)
    try:
        return (await session.execute(statement.limit(1))).scalar_one_or_none() is not None
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None


def _rematerialize_filter(item: object, columns_by_name: dict[str, CatalogColumn]) -> PostgresFilter:
    if not isinstance(item, dict):
        raise _MetricRequirementError("definition_invalid")
    column = _column_from_contract(item.get("column"), columns_by_name=columns_by_name)
    raw_filter = item.get("filter")
    if not isinstance(raw_filter, dict):
        raise _MetricRequirementError("definition_invalid")
    payload = {"column_id": str(column.id), **raw_filter}
    try:
        return PostgresFilter.model_validate(payload)
    except ValidationError as exc:
        raise _MetricRequirementError("definition_invalid") from exc


async def _rematerialize_version(
    session: AsyncSession,
    *,
    definition: MetricDefinition,
    version: MetricDefinitionVersion,
    owner_user_id: uuid.UUID,
    settings: Settings,
) -> ResolvedMetricDefinition:
    try:
        context = await get_postgres_chat_catalog_context_for_owner(
            session, source_id=definition.source_id, owner_user_id=owner_user_id
        )
    except (CatalogQueryContextError, CatalogPersistenceError):
        # A transient catalog read failure must fail closed for use, but it is
        # not evidence that an owner-confirmed definition became stale.
        raise MetricCatalogUnavailableError() from None
    except NotFoundError:
        raise _MetricRequirementError("source_unavailable") from None
    # The helper requires an asset when a requested ID is supplied. Resolve
    # the old structural identity from the immutable payload, then locate the
    # same literal schema/table in the current selected snapshot.
    payload = version.definition_json
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise _MetricRequirementError("definition_invalid")
    asset_contract = payload.get("asset")
    if not isinstance(asset_contract, dict):
        raise _MetricRequirementError("definition_invalid")
    expected_schema = asset_contract.get("schema_name")
    expected_table = asset_contract.get("table_name")
    current_asset = next(
        (
            item
            for item in context.assets
            if item.schema_name == expected_schema and item.table_name == expected_table
        ),
        None,
    )
    if current_asset is None:
        raise _MetricRequirementError("asset_unavailable")
    # Re-load only the current matching asset; this is intentionally a fresh
    # owner/snapshot/scope check rather than reusing historical IDs.
    try:
        current_columns = await get_postgres_chat_asset_columns_for_owner(
            session,
            source_id=definition.source_id,
            snapshot_id=context.snapshot.id,
            asset_id=current_asset.id,
            owner_user_id=owner_user_id,
        )
    except (CatalogQueryContextError, CatalogPersistenceError):
        raise MetricCatalogUnavailableError() from None
    except NotFoundError:
        raise _MetricRequirementError("asset_unavailable") from None
    columns_by_name = {column.column_name: column for column in current_columns}
    aggregation_payload = payload.get("aggregation")
    if not isinstance(aggregation_payload, dict):
        raise _MetricRequirementError("definition_invalid")
    operation_raw = aggregation_payload.get("operation")
    if not isinstance(operation_raw, str):
        raise _MetricRequirementError("definition_invalid")
    aggregation_column = None
    if aggregation_payload.get("column") is not None:
        aggregation_column = _column_from_contract(
            aggregation_payload.get("column"), columns_by_name=columns_by_name
        )
    operation = _operation_from_request(operation_raw, aggregation_column.id if aggregation_column else None)
    filters_raw = payload.get("fixed_filters")
    if not isinstance(filters_raw, list) or len(filters_raw) > 50:
        raise _MetricRequirementError("definition_invalid")
    fixed_filters = tuple(
        _rematerialize_filter(item, columns_by_name) for item in filters_raw
    )
    default_column = None
    if payload.get("default_date_column") is not None:
        default_column = _column_from_contract(
            payload.get("default_date_column"), columns_by_name=columns_by_name
        )
    try:
        await validate_postgres_query_plan(
            session,
            owner_user_id=owner_user_id,
            source_id=definition.source_id,
            snapshot_id=context.snapshot.id,
            plan=_base_plan(
                asset_id=current_asset.id,
                operation=operation,
                aggregation_column_id=(aggregation_column.id if aggregation_column else None),
                filters=fixed_filters,
                default_date_column_id=(default_column.id if default_column else None),
                default_time_zone=version.default_time_zone,
            ),
            settings=settings,
        )
    except (PostgresQueryPlanValidationError, ValidationError, ValueError) as exc:
        raise _MetricRequirementError("definition_incompatible") from exc
    return ResolvedMetricDefinition(
        metric_id=definition.id,
        metric_version_id=version.id,
        version_number=version.version_number,
        source_id=definition.source_id,
        snapshot_id=context.snapshot.id,
        asset_id=current_asset.id,
        asset_schema_name=current_asset.schema_name,
        asset_table_name=current_asset.table_name,
        display_name=version.display_name,
        description=version.description,
        aggregation=operation,
        aggregation_column_id=aggregation_column.id if aggregation_column else None,
        aggregation_column_name=aggregation_column.column_name if aggregation_column else None,
        fixed_filters=fixed_filters,
        unit_label=version.unit_label,
        default_date_column_id=default_column.id if default_column else None,
        default_date_column_name=default_column.column_name if default_column else None,
        default_time_zone=version.default_time_zone,
        confirmed_at=version.confirmed_at,
        confirmed_by_user_id=version.confirmed_by_user_id,
        allowed_column_ids=frozenset(column.id for column in current_columns),
    )


async def _mark_needs_review(
    session: AsyncSession,
    *,
    definition: MetricDefinition,
    reason: str,
) -> None:
    if definition.lifecycle_state is not MetricLifecycleState.ACTIVE:
        return
    definition.lifecycle_state = MetricLifecycleState.NEEDS_REVIEW
    definition.needs_review_at = _now()
    definition.needs_review_reason = reason[:64]
    try:
        await session.commit()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None


async def create_metric_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: MetricDefinitionWriteRequest,
    settings: Settings | None = None,
) -> MetricDefinitionView:
    """Create a logical metric and immutable owner-confirmed version one."""

    effective_settings = settings or get_settings()
    context, asset, columns, payload = await _validate_confirmation_request(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
        request=request,
        settings=effective_settings,
    )
    normalized_name = normalize_metric_name(request.name)
    if await _active_name_conflict(
        session, source_id=source_id, normalized_name=normalized_name
    ):
        raise MetricConflictError("an active metric with this name already exists for the source")
    metric_id = uuid.uuid4()
    version_id = uuid.uuid4()
    definition = MetricDefinition(
        id=metric_id,
        source_id=source_id,
        owner_user_id=owner_user_id,
        display_name=request.name,
        normalized_name=normalized_name,
        lifecycle_state=MetricLifecycleState.ACTIVE,
        current_version_number=1,
    )
    version = MetricDefinitionVersion(
        id=version_id,
        source_id=source_id,
        metric_definition_id=metric_id,
        version_number=1,
        display_name=request.name,
        description=request.description,
        confirmed_snapshot_id=context.snapshot.id,
        asset_id=asset.id,
        aggregate_operation=_metric_enum(request.aggregation.operation),
        aggregate_column_id=request.aggregation.column_id,
        unit_label=request.unit_label,
        default_date_column_id=request.default_date_column_id,
        default_time_zone=request.default_time_zone,
        definition_json=payload,
        confirmed_by_user_id=owner_user_id,
        confirmed_at=_now(),
    )
    try:
        session.add_all((definition, version))
        await session.commit()
    except IntegrityError:
        await _rollback_quietly(session)
        raise MetricConflictError("an active metric with this name already exists for the source") from None
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None
    return _view_from_rows(definition, version)


async def create_metric_definition_version_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: MetricDefinitionWriteRequest,
    settings: Settings | None = None,
) -> MetricDefinitionView:
    """Save a complete new immutable version after a fresh owner confirmation."""

    effective_settings = settings or get_settings()
    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=owner_user_id,
        lock=True,
    )
    if definition.lifecycle_state is MetricLifecycleState.RETIRED:
        await _rollback_quietly(session)
        raise MetricRetiredError()
    context, asset, columns, payload = await _validate_confirmation_request(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
        request=request,
        settings=effective_settings,
    )
    normalized_name = normalize_metric_name(request.name)
    if await _active_name_conflict(
        session,
        source_id=source_id,
        normalized_name=normalized_name,
        except_metric_id=definition.id,
    ):
        await _rollback_quietly(session)
        raise MetricConflictError("an active metric with this name already exists for the source")
    next_version = definition.current_version_number + 1
    version = MetricDefinitionVersion(
        id=uuid.uuid4(),
        source_id=source_id,
        metric_definition_id=definition.id,
        version_number=next_version,
        display_name=request.name,
        description=request.description,
        confirmed_snapshot_id=context.snapshot.id,
        asset_id=asset.id,
        aggregate_operation=_metric_enum(request.aggregation.operation),
        aggregate_column_id=request.aggregation.column_id,
        unit_label=request.unit_label,
        default_date_column_id=request.default_date_column_id,
        default_time_zone=request.default_time_zone,
        definition_json=payload,
        confirmed_by_user_id=owner_user_id,
        confirmed_at=_now(),
    )
    definition.display_name = request.name
    definition.normalized_name = normalized_name
    definition.current_version_number = next_version
    definition.lifecycle_state = MetricLifecycleState.ACTIVE
    definition.needs_review_at = None
    definition.needs_review_reason = None
    try:
        session.add(version)
        await session.commit()
    except IntegrityError:
        await _rollback_quietly(session)
        raise MetricConflictError("an active metric with this name already exists for the source") from None
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None
    return _view_from_rows(definition, version)


async def retire_metric_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> MetricDefinitionView:
    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=owner_user_id,
        lock=True,
    )
    if definition.lifecycle_state is not MetricLifecycleState.RETIRED:
        definition.lifecycle_state = MetricLifecycleState.RETIRED
        definition.retired_at = _now()
        definition.retired_by_user_id = owner_user_id
        try:
            await session.commit()
        except SQLAlchemyError:
            await _rollback_quietly(session)
            raise MetricPersistenceError() from None
    version = await _version_for_definition(
        session, definition=definition, version_number=definition.current_version_number
    )
    return _view_from_rows(definition, version)


def _state_for(definition: MetricDefinition) -> Literal["valid", "needs_review", "retired"]:
    if definition.lifecycle_state is MetricLifecycleState.RETIRED:
        return "retired"
    if definition.lifecycle_state is MetricLifecycleState.NEEDS_REVIEW:
        return "needs_review"
    return "valid"


def _view_from_rows(
    definition: MetricDefinition,
    version: MetricDefinitionVersion,
    *,
    resolved: ResolvedMetricDefinition | None = None,
) -> MetricDefinitionView:
    payload = version.definition_json if isinstance(version.definition_json, dict) else {}
    aggregation_payload = payload.get("aggregation") if isinstance(payload, dict) else None
    aggregate_contract = (
        aggregation_payload.get("column") if isinstance(aggregation_payload, dict) else None
    )
    default_contract = payload.get("default_date_column") if isinstance(payload, dict) else None
    filters: list[PostgresFilter] = []
    if isinstance(payload.get("fixed_filters"), list):
        for raw in payload["fixed_filters"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("filter"), dict) or not isinstance(raw.get("column"), dict):
                continue
            stored_id = raw["column"].get("column_id")
            try:
                filters.append(PostgresFilter.model_validate({"column_id": stored_id, **raw["filter"]}))
            except (ValidationError, TypeError, ValueError):
                continue
    return MetricDefinitionView(
        metric_id=definition.id,
        version_number=version.version_number,
        name=version.display_name,
        description=version.description,
        snapshot_id=version.confirmed_snapshot_id,
        asset_id=version.asset_id,
        aggregation_operation=_operation_to_public(version.aggregate_operation),
        aggregation_column_id=version.aggregate_column_id,
        aggregation_column_name=(
            aggregate_contract.get("column_name") if isinstance(aggregate_contract, dict) else None
        ),
        fixed_filters=tuple(filters),
        unit_label=version.unit_label,
        default_date_column_id=version.default_date_column_id,
        default_date_column_name=(
            default_contract.get("column_name") if isinstance(default_contract, dict) else None
        ),
        default_time_zone=version.default_time_zone,
        confirmed_by_user_id=version.confirmed_by_user_id,
        confirmed_at=version.confirmed_at,
        state=_state_for(definition),
        needs_review_reason=definition.needs_review_reason,
        retired_at=definition.retired_at,
        current_catalog=(
            MetricCurrentCatalogView(
                snapshot_id=resolved.snapshot_id,
                asset_id=resolved.asset_id,
                asset_schema_name=resolved.asset_schema_name,
                asset_table_name=resolved.asset_table_name,
                aggregation_column_id=resolved.aggregation_column_id,
                aggregation_column_name=resolved.aggregation_column_name,
                fixed_filters=resolved.fixed_filters,
                default_date_column_id=resolved.default_date_column_id,
                default_date_column_name=resolved.default_date_column_name,
                default_time_zone=resolved.default_time_zone,
            )
            if resolved is not None
            else None
        ),
    )


async def get_metric_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    settings: Settings | None = None,
) -> MetricDefinitionView:
    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=owner_user_id,
    )
    version = await _version_for_definition(
        session, definition=definition, version_number=definition.current_version_number
    )
    resolved: ResolvedMetricDefinition | None = None
    if definition.lifecycle_state is MetricLifecycleState.ACTIVE:
        try:
            resolved = await _rematerialize_version(
                session,
                definition=definition,
                version=version,
                owner_user_id=owner_user_id,
                settings=settings or get_settings(),
            )
        except MetricCatalogUnavailableError:
            # Browsing a definition should not turn a temporary catalog outage
            # into a permanent review state.  Resolution/execution remains
            # fail-closed and raises this error instead.
            pass
        except _MetricRequirementError as exc:
            await _mark_needs_review(session, definition=definition, reason=exc.reason)
            # Refresh to make the persisted lifecycle visible in the response.
            definition = await _definition_for_owner(
                session,
                source_id=source_id,
                metric_id=metric_id,
                owner_user_id=owner_user_id,
            )
    return _view_from_rows(definition, version, resolved=resolved)


async def list_metric_definitions_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    include_retired: bool = True,
    settings: Settings | None = None,
) -> tuple[MetricDefinitionView, ...]:
    # A source with no metric rows is still an owned resource. Authorize it
    # before the list query so another user cannot distinguish an existing
    # source with zero definitions from a nonexistent one.
    await get_source_for_owner(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
    )
    try:
        definitions = (
            await session.execute(
                select(MetricDefinition)
                .where(
                    MetricDefinition.source_id == source_id,
                    MetricDefinition.owner_user_id == owner_user_id,
                    (
                        True
                        if include_retired
                        else MetricDefinition.lifecycle_state != MetricLifecycleState.RETIRED
                    ),
                )
                .order_by(MetricDefinition.display_name, MetricDefinition.id)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None
    views: list[MetricDefinitionView] = []
    for definition in definitions:
        version = await _version_for_definition(
            session, definition=definition, version_number=definition.current_version_number
        )
        resolved: ResolvedMetricDefinition | None = None
        if definition.lifecycle_state is MetricLifecycleState.ACTIVE:
            try:
                resolved = await _rematerialize_version(
                    session,
                    definition=definition,
                    version=version,
                    owner_user_id=owner_user_id,
                    settings=settings or get_settings(),
                )
            except MetricCatalogUnavailableError:
                # Do not hide the entire list or mutate lifecycle state during
                # a temporary catalog outage.  A later use is still blocked.
                pass
            except _MetricRequirementError as exc:
                await _mark_needs_review(session, definition=definition, reason=exc.reason)
        views.append(_view_from_rows(definition, version, resolved=resolved))
    return tuple(views)


async def list_active_metric_definitions_readonly(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    settings: Settings | None = None,
) -> tuple[ResolvedMetricDefinition, ...]:
    """ACTIVE, currently resolvable metrics of one owned source, with **no writes and no commit** (SPEC-08 B7).

    ``list_metric_definitions_for_owner`` marks a stale definition NEEDS_REVIEW as it goes, so building a prompt
    could change data. Here a definition that no longer resolves is simply left out; the existing listing
    endpoint remains the place that marks it.
    """
    await get_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)
    try:
        definitions = (
            await session.execute(
                select(MetricDefinition)
                .where(
                    MetricDefinition.source_id == source_id,
                    MetricDefinition.owner_user_id == owner_user_id,
                    MetricDefinition.lifecycle_state == MetricLifecycleState.ACTIVE,
                )
                .order_by(MetricDefinition.display_name, MetricDefinition.id)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None
    resolved: list[ResolvedMetricDefinition] = []
    for definition in definitions:
        version = await _version_for_definition(
            session, definition=definition, version_number=definition.current_version_number
        )
        try:
            resolved.append(
                await _rematerialize_version(
                    session,
                    definition=definition,
                    version=version,
                    owner_user_id=owner_user_id,
                    settings=settings or get_settings(),
                )
            )
        except (MetricCatalogUnavailableError, _MetricRequirementError):
            continue
    return tuple(resolved)


async def list_metric_definition_versions_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> tuple[MetricDefinitionView, ...]:
    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=owner_user_id,
    )
    try:
        versions = (
            await session.execute(
                select(MetricDefinitionVersion)
                .where(
                    MetricDefinitionVersion.source_id == source_id,
                    MetricDefinitionVersion.metric_definition_id == definition.id,
                )
                .order_by(MetricDefinitionVersion.version_number.desc())
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise MetricPersistenceError() from None
    return tuple(_view_from_rows(definition, version) for version in versions)


async def resolve_metric_definition_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    version_number: int | None = None,
    require_current: bool = True,
    settings: Settings | None = None,
) -> ResolvedMetricDefinition:
    """Return a current, executable metric or fail closed without remapping it."""

    definition = await _definition_for_owner(
        session,
        source_id=source_id,
        metric_id=metric_id,
        owner_user_id=owner_user_id,
    )
    if definition.lifecycle_state is MetricLifecycleState.RETIRED:
        raise MetricRetiredError()
    if definition.lifecycle_state is MetricLifecycleState.NEEDS_REVIEW:
        raise MetricNeedsReviewError()
    selected_version = version_number or definition.current_version_number
    if require_current and selected_version != definition.current_version_number:
        raise MetricNeedsReviewError("metric definition changed; use its current owner-confirmed version")
    version = await _version_for_definition(
        session, definition=definition, version_number=selected_version
    )
    try:
        return await _rematerialize_version(
            session,
            definition=definition,
            version=version,
            owner_user_id=owner_user_id,
            settings=settings or get_settings(),
        )
    except MetricCatalogUnavailableError:
        raise
    except _MetricRequirementError as exc:
        await _mark_needs_review(session, definition=definition, reason=exc.reason)
        raise MetricNeedsReviewError() from None


async def verify_metric_pin_for_execution(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    metric_id: uuid.UUID,
    version_number: int,
    snapshot_id: uuid.UUID,
    metric_version_id: uuid.UUID | None = None,
    settings: Settings | None = None,
) -> bool:
    """Recheck a planner pin immediately around the execution boundary."""

    try:
        resolved = await resolve_metric_definition_for_owner(
            session,
            source_id=source_id,
            metric_id=metric_id,
            owner_user_id=owner_user_id,
            version_number=version_number,
            require_current=True,
            settings=settings,
        )
    except (MetricServiceError, NotFoundError):
        return False
    return (
        resolved.snapshot_id == snapshot_id
        and (metric_version_id is None or resolved.metric_version_id == metric_version_id)
    )


async def match_metric_definition_for_question(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    question: str,
) -> MetricMatch | None:
    """Find one active display name without letting an LLM choose a metric."""

    try:
        normalized_question = normalize_metric_name(question)
    except MetricValidationError:
        return None
    try:
        definitions = (
            await session.execute(
                select(MetricDefinition)
                .where(
                    MetricDefinition.source_id == source_id,
                    MetricDefinition.owner_user_id == owner_user_id,
                    MetricDefinition.lifecycle_state == MetricLifecycleState.ACTIVE,
                )
                .order_by(MetricDefinition.normalized_name, MetricDefinition.id)
                .limit(_MAX_METRIC_MATCHES + 1)
            )
        ).scalars().all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        return MetricMatch(kind="unavailable", message="confirmed metrics are unavailable right now")
    if len(definitions) > _MAX_METRIC_MATCHES:
        return MetricMatch(kind="ambiguous", message="Please name the confirmed metric more specifically.")
    matches = [
        definition
        for definition in definitions
        if normalized_question == definition.normalized_name
        or re.search(
            rf"(?<!\w){re.escape(definition.normalized_name)}(?!\w)", normalized_question
        )
    ]
    if not matches:
        return None
    # Prefer the exact full-question name, then require a unique phrase match.
    exact = [item for item in matches if item.normalized_name == normalized_question]
    selected = exact if exact else matches
    if len(selected) != 1:
        return MetricMatch(kind="ambiguous", message="Please specify one confirmed metric by name.")
    metric = selected[0]
    return MetricMatch(
        kind="matched",
        metric_id=metric.id,
        version_number=metric.current_version_number,
    )


def _intent_value(intent: Any, name: str, default: Any = None) -> Any:
    if isinstance(intent, dict):
        return intent.get(name, default)
    return getattr(intent, name, default)


def expand_metric_query_intent(
    resolved: ResolvedMetricDefinition,
    intent: Any,
    settings: Settings,
) -> PostgresQueryPlan:
    """Expand a narrow model modifier envelope into the existing safe plan."""

    if _intent_value(intent, "metric_id") != resolved.metric_id:
        raise MetricValidationError("metric planner selected a different metric")
    if _intent_value(intent, "version_number") != resolved.version_number:
        raise MetricValidationError("metric planner selected a different metric version")
    # A joined metric has stricter asset qualification and relationship-pin
    # checks.  It must enter through the dedicated expansion below rather
    # than quietly treating a model-supplied relationship as decorative data.
    if _intent_value(intent, "join") is not None:
        raise MetricValidationError("joined metric modifiers require an approved relationship")
    raw_filters = _intent_value(intent, "additional_filters", ())
    raw_group_by = _intent_value(intent, "group_by", ())
    raw_sort = _intent_value(intent, "sort")
    raw_limit = _intent_value(intent, "limit")
    try:
        additional_filters = tuple(PostgresFilter.model_validate(item) for item in raw_filters)
        group_by = tuple(PostgresGroupDimension.model_validate(item) for item in raw_group_by)
        sort = PostgresSort.model_validate(raw_sort) if raw_sort is not None else None
    except (ValidationError, TypeError, ValueError) as exc:
        raise MetricValidationError("metric planner modifiers are invalid") from exc
    if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
        raise MetricValidationError("metric planner limit is invalid")
    if raw_limit > settings.postgres_query_max_result_limit:
        raise MetricValidationError("metric planner limit exceeds the configured result limit")
    references = [filter_item.column_id for filter_item in additional_filters]
    references.extend(item.column_id for item in group_by)
    if any(column_id not in resolved.allowed_column_ids for column_id in references):
        raise MetricValidationError("metric planner selected a column outside the confirmed table")
    # Time defaults are prompt/planning context, not an implicit aggregation
    # instruction. A scalar question must remain scalar; a planner may choose
    # an explicit permitted temporal dimension when the user asks for it.
    # Never infer a bucket or business time basis merely because a definition
    # records a default date column/timezone.
    effective_group_by = group_by
    return PostgresQueryPlan(
        asset_id=resolved.asset_id,
        aggregations=(
            PostgresAggregation(
                operation=resolved.aggregation,
                column_id=resolved.aggregation_column_id,
            ),
        ),
        # Stored fixed predicates are inserted first and cannot be removed or
        # overwritten by a model modifier. Extra filters only narrow further.
        filters=(*resolved.fixed_filters, *additional_filters),
        group_by=effective_group_by,
        sort=sort,
        limit=raw_limit,
    )


def _relationship_value(relationship: Any, name: str, default: Any = None) -> Any:
    """Read a service-resolved relationship without accepting ORM rows.

    The planner passes ``ResolvedRelationshipDefinition``.  Keeping this
    small duck-typed boundary makes the deterministic expansion testable with
    narrow service adapters while never giving an LLM or API request a path
    to create its own relationship facts.
    """

    if isinstance(relationship, dict):
        return relationship.get(name, default)
    return getattr(relationship, name, default)


def _qualified_metric_join_modifiers(
    *,
    resolved: ResolvedMetricDefinition,
    relationship: Any,
    raw_filters: Any,
    raw_group_by: Any,
    raw_sort: Any,
) -> tuple[tuple[PostgresFilter, ...], tuple[PostgresGroupDimension, ...], PostgresSort | None]:
    """Validate user-requested modifiers across one approved lookup edge.

    Metric fields stay tied to ``resolved.asset_id``.  Lookup fields can only
    be current, owner-authorized columns from the resolved relationship's
    lookup asset.  This function does not validate SQL: the existing compiler
    still resolves every ID against the current catalog immediately before
    execution.
    """

    try:
        filters = tuple(PostgresFilter.model_validate(item) for item in raw_filters)
        group_by = tuple(PostgresGroupDimension.model_validate(item) for item in raw_group_by)
        sort = PostgresSort.model_validate(raw_sort) if raw_sort is not None else None
    except (ValidationError, TypeError, ValueError) as exc:
        raise MetricValidationError("metric planner modifiers are invalid") from exc

    lookup_asset_id = _relationship_value(relationship, "lookup_asset_id")
    relationship_join_type = _relationship_value(relationship, "join_type")
    base_allowed = _relationship_value(relationship, "allowed_base_column_ids", frozenset())
    lookup_allowed = _relationship_value(relationship, "allowed_lookup_column_ids", frozenset())
    if (
        not isinstance(lookup_asset_id, uuid.UUID)
        or relationship_join_type not in {"inner", "left"}
        or not isinstance(base_allowed, (set, frozenset, tuple))
        or not isinstance(lookup_allowed, (set, frozenset, tuple))
    ):
        raise MetricValidationError("approved relationship context is invalid")
    base_allowed_ids = set(base_allowed)
    lookup_allowed_ids = set(lookup_allowed)
    # The metric resolver and relationship resolver must agree about the
    # current base table.  Requiring the intersection prevents a stale
    # relationship adapter from widening the metric's base-column authority.
    base_allowed_ids.intersection_update(resolved.allowed_column_ids)

    def ensure_reference(*, asset_id: uuid.UUID | None, column_id: uuid.UUID, field: str) -> None:
        if asset_id is None:
            raise MetricValidationError("joined metric references require an asset identifier")
        if asset_id == resolved.asset_id:
            if column_id not in base_allowed_ids:
                raise MetricValidationError("metric planner selected a base column outside the confirmed context")
            return
        if asset_id == lookup_asset_id:
            if column_id not in lookup_allowed_ids:
                raise MetricValidationError("metric planner selected a lookup column outside the approved relationship")
            return
        raise MetricValidationError(f"metric planner selected a {field} column outside the approved relationship")

    for item in filters:
        ensure_reference(asset_id=item.asset_id, column_id=item.column_id, field="filter")
        if item.asset_id == resolved.asset_id and item.placement is not None:
            # The compiler does not let a base predicate move into ON.  Keep
            # metric-generated base predicates even narrower: no placement
            # vocabulary is necessary for their ordinary WHERE semantics.
            raise MetricValidationError("base metric filters cannot specify join placement")
        if (
            item.asset_id == lookup_asset_id
            and relationship_join_type == "left"
            and item.placement not in {"where", "join_on"}
        ):
            raise MetricValidationError("LEFT join lookup filters require explicit placement")
        if item.asset_id == lookup_asset_id and relationship_join_type == "inner" and item.placement not in {
            None,
            "where",
            "join_on",
        }:
            raise MetricValidationError("lookup filter placement is invalid")
    for item in group_by:
        ensure_reference(asset_id=item.asset_id, column_id=item.column_id, field="grouping")
    return filters, group_by, sort


def expand_metric_relationship_query_intent(
    resolved: ResolvedMetricDefinition,
    relationship: Any,
    intent: Any,
    settings: Settings,
) -> PostgresQueryPlan:
    """Expand one metric through one pinned, approved same-source lookup.

    This is intentionally not a generic multi-table planner.  The aggregate
    and all fixed predicates remain on the metric's designated base table;
    only grouping and extra narrowing filters may reference the lookup side
    of the single approved relationship.
    """

    if _intent_value(intent, "metric_id") != resolved.metric_id:
        raise MetricValidationError("metric planner selected a different metric")
    if _intent_value(intent, "version_number") != resolved.version_number:
        raise MetricValidationError("metric planner selected a different metric version")
    raw_join = _intent_value(intent, "join")
    try:
        join = PostgresApprovedJoin.model_validate(raw_join)
    except (ValidationError, TypeError, ValueError) as exc:
        raise MetricValidationError("metric planner relationship is invalid") from exc

    relationship_id = _relationship_value(relationship, "relationship_id")
    relationship_version_id = _relationship_value(relationship, "relationship_version_id")
    relationship_source_id = _relationship_value(relationship, "source_id")
    relationship_snapshot_id = _relationship_value(relationship, "snapshot_id")
    relationship_base_asset_id = _relationship_value(relationship, "base_asset_id")
    relationship_lookup_asset_id = _relationship_value(relationship, "lookup_asset_id")
    relationship_join_type = _relationship_value(relationship, "join_type")
    if (
        relationship_id != join.relationship_id
        or relationship_version_id != join.relationship_version_id
        or relationship_source_id != resolved.source_id
        or relationship_snapshot_id != resolved.snapshot_id
        or relationship_base_asset_id != resolved.asset_id
        or relationship_lookup_asset_id != join.lookup_asset_id
        or relationship_join_type != join.join_type
    ):
        raise MetricValidationError("approved relationship does not match the confirmed metric context")

    raw_filters = _intent_value(intent, "additional_filters", ())
    raw_group_by = _intent_value(intent, "group_by", ())
    raw_sort = _intent_value(intent, "sort")
    raw_limit = _intent_value(intent, "limit")
    if (
        not isinstance(raw_limit, int)
        or isinstance(raw_limit, bool)
        or raw_limit < 1
        or raw_limit > settings.postgres_query_max_result_limit
    ):
        raise MetricValidationError("metric planner limit is invalid")
    additional_filters, group_by, sort = _qualified_metric_join_modifiers(
        resolved=resolved,
        relationship=relationship,
        raw_filters=raw_filters,
        raw_group_by=raw_group_by,
        raw_sort=raw_sort,
    )
    fixed_filters = tuple(
        item.model_copy(update={"asset_id": resolved.asset_id, "placement": None})
        for item in resolved.fixed_filters
    )
    aggregation_asset_id = (
        resolved.asset_id if resolved.aggregation_column_id is not None else None
    )
    return PostgresQueryPlan(
        asset_id=resolved.asset_id,
        join=join,
        aggregations=(
            PostgresAggregation(
                operation=resolved.aggregation,
                column_id=resolved.aggregation_column_id,
                asset_id=aggregation_asset_id,
            ),
        ),
        # Fixed predicates are copied first from the immutable metric
        # contract.  A lookup predicate is never allowed to replace or move a
        # base fixed predicate into the ON clause.
        filters=(*fixed_filters, *additional_filters),
        group_by=group_by,
        sort=sort,
        limit=raw_limit,
    )


def metric_provenance(resolved: ResolvedMetricDefinition) -> PostgresMetricProvenance:
    """Return safe presentation/audit facts without fixed-filter values."""

    return PostgresMetricProvenance(
        metric_id=resolved.metric_id,
        version_number=resolved.version_number,
        name=resolved.display_name,
        aggregation=resolved.aggregation,
        aggregation_column_name=resolved.aggregation_column_name,
        unit_label=resolved.unit_label,
        default_date_column_name=resolved.default_date_column_name,
        default_time_zone=resolved.default_time_zone,
        fixed_filter_count=len(resolved.fixed_filters),
    )


__all__ = [
    "MetricCatalogUnavailableError",
    "MetricConflictError",
    "MetricCurrentCatalogView",
    "MetricDefinitionView",
    "MetricMatch",
    "MetricNeedsReviewError",
    "MetricPersistenceError",
    "MetricRetiredError",
    "MetricServiceError",
    "MetricValidationError",
    "ResolvedMetricDefinition",
    "create_metric_definition_for_owner",
    "create_metric_definition_version_for_owner",
    "expand_metric_relationship_query_intent",
    "expand_metric_query_intent",
    "get_metric_definition_for_owner",
    "list_active_metric_definitions_readonly",
    "list_metric_definition_versions_for_owner",
    "list_metric_definitions_for_owner",
    "match_metric_definition_for_question",
    "metric_provenance",
    "normalize_metric_name",
    "resolve_metric_definition_for_owner",
    "retire_metric_definition_for_owner",
    "verify_metric_pin_for_execution",
]
