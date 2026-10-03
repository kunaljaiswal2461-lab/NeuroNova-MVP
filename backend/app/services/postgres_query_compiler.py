"""Authorize, validate, and compile the narrow PostgreSQL query language.

Nothing in this module opens a client connection or executes SQL.  It turns a
strict ``PostgresQueryPlan`` plus trusted source/snapshot/actor context into a
single parameterized statement for the Stage 4B executor boundary.

The existing ``agentic_engine.conversational`` QueryPlan remains exclusively
for uploaded-file / Polars queries and must not be imported here.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.catalog import CatalogColumn
from app.services.catalog_service import (
    resolve_postgres_query_catalog_context_for_owner,
)
from app.services.postgres_query_plan import (
    BooleanValue,
    DateValue,
    DecimalValue,
    IntegerValue,
    NumberValue,
    PostgresApprovedJoin,
    PostgresAggregation,
    PostgresFilter,
    PostgresGroupDimension,
    PostgresQueryPlan,
    TextValue,
    TimestampValue,
    TimestamptzValue,
    TypedPostgresValue,
    UUIDValue,
)


_INTEGER_TYPE_RANGES: dict[str, tuple[int, int]] = {
    "int2": (-32_768, 32_767),
    "int4": (-2_147_483_648, 2_147_483_647),
    "int8": (-9_223_372_036_854_775_808, 9_223_372_036_854_775_807),
}
_NUMERIC_NATIVE_TYPES = frozenset((*_INTEGER_TYPE_RANGES, "numeric", "float4", "float8"))
_TEXT_NATIVE_TYPES = frozenset(("text", "varchar", "bpchar"))
_SUPPORTED_NATIVE_TYPES = frozenset(
    (*_NUMERIC_NATIVE_TYPES, *_TEXT_NATIVE_TYPES, "bool", "uuid", "date", "timestamp", "timestamptz")
)
_CATEGORY_BY_NATIVE_TYPE: dict[str, str] = {
    **{type_name: "integer" for type_name in _INTEGER_TYPE_RANGES},
    "numeric": "decimal",
    "float4": "number",
    "float8": "number",
    **{type_name: "text" for type_name in _TEXT_NATIVE_TYPES},
    "bool": "boolean",
    "uuid": "uuid",
    "date": "date",
    "timestamp": "timestamp",
    "timestamptz": "timestamptz",
}
_SQL_TYPE_BY_NATIVE_TYPE: dict[str, str] = {
    native_type: f"pg_catalog.{native_type}" for native_type in _SUPPORTED_NATIVE_TYPES
}
_FILTER_OPERATORS_BY_CATEGORY: dict[str, frozenset[str]] = {
    "integer": frozenset({"eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in", "is_null", "is_not_null"}),
    "decimal": frozenset({"eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in", "is_null", "is_not_null"}),
    "number": frozenset({"eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in", "is_null", "is_not_null"}),
    "text": frozenset({"eq", "neq", "in", "not_in", "contains", "not_contains", "is_null", "is_not_null"}),
    "boolean": frozenset({"eq", "neq", "in", "not_in", "is_null", "is_not_null"}),
    "uuid": frozenset({"eq", "neq", "in", "not_in", "is_null", "is_not_null"}),
    "date": frozenset({"eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in", "is_null", "is_not_null"}),
    "timestamp": frozenset({"eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in", "is_null", "is_not_null"}),
    "timestamptz": frozenset({"eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in", "is_null", "is_not_null"}),
}
_BUCKETS_BY_CATEGORY: dict[str, frozenset[str]] = {
    "date": frozenset({"day", "week", "month", "quarter", "year"}),
    "timestamp": frozenset({"hour", "day", "week", "month", "quarter", "year"}),
    "timestamptz": frozenset({"hour", "day", "week", "month", "quarter", "year"}),
}
_VALIDATED_PLAN_SEAL = object()
_COMPILED_QUERY_SEAL = object()


class PostgresQueryPlanValidationError(ValueError):
    """A safe structural or semantic reason a plan is not compilable."""

    def __init__(
        self,
        *,
        field: str,
        reason: str,
        column_id: UUID | None = None,
        actual_type: str | None = None,
    ) -> None:
        self.field = field
        self.reason = reason
        self.column_id = column_id
        self.actual_type = actual_type
        details = [reason]
        if column_id is not None:
            details.append(f"column {column_id}")
        if actual_type is not None:
            details.append(f"type {actual_type}")
        super().__init__(f"{field}: " + "; ".join(details))

    def as_dict(self) -> dict[str, str | None]:
        return {
            "field": self.field,
            "reason": self.reason,
            "column_id": str(self.column_id) if self.column_id is not None else None,
            "actual_type": self.actual_type,
        }


@dataclass(frozen=True)
class ResolvedPostgresColumn:
    """Compiler-safe catalog facts; comments and formatted type text are absent."""

    id: UUID
    asset_id: UUID
    name: str
    native_type_name: str
    category: str
    nullable: bool


@dataclass(frozen=True)
class ResolvedPostgresJoinColumnPair:
    """Current-catalog column facts for one ordered equality-key pair."""

    base_column: ResolvedPostgresColumn
    lookup_column: ResolvedPostgresColumn


@dataclass(frozen=True)
class ResolvedPostgresJoin:
    """A relationship-service-authorized join, safe for deterministic SQL."""

    relationship_id: UUID
    relationship_version_id: UUID
    version_number: int
    join_type: Literal["inner", "left"]
    lookup_asset_id: UUID
    lookup_schema_name: str
    lookup_table_name: str
    column_pairs: tuple[ResolvedPostgresJoinColumnPair, ...]
    foreign_key_constraint_name: str
    target_key_constraint_name: str
    target_key_kind: Literal["primary_key", "unique"]
    target_key_column_names: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedPostgresQueryPlan:
    """A plan bound to trusted owner/current-catalog context, not executable."""

    source_id: UUID
    snapshot_id: UUID
    asset_id: UUID
    schema_name: str
    table_name: str
    current_scope_fingerprint: str
    columns: Mapping[UUID, ResolvedPostgresColumn]
    plan: PostgresQueryPlan = field(repr=False)
    join: ResolvedPostgresJoin | None = None
    # The compiler is intentionally module-private. This marker makes an
    # accidental hand-built dataclass fail closed rather than bypassing the
    # resolver and configured complexity checks on its way there.
    _seal: object | None = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:
        return (
            "ValidatedPostgresQueryPlan("
            f"source_id={self.source_id!r}, snapshot_id={self.snapshot_id!r}, "
            f"asset_id={self.asset_id!r}, column_count={len(self.columns)})"
        )


@dataclass(frozen=True)
class ExpectedPostgresOutputColumn:
    """A result-column description for Stage 4B's existing result adapter."""

    alias: str
    logical_type: str
    postgres_type: str
    source_column_id: UUID | None
    source_column_name: str | None
    source_asset_id: UUID | None = None
    operation: str | None = None
    bucket: str | None = None
    time_zone: str | None = None


@dataclass(frozen=True)
class CompiledPostgresOrdering:
    alias: str
    direction: Literal["asc", "desc"]


@dataclass(frozen=True)
class PostgresCalculationSemantics:
    """Explicit facts that an answer/presentation layer must not infer."""

    filters_apply_before_aggregation: bool
    post_aggregation_filters_supported: bool
    count_all_is_row_count: bool
    count_column_excludes_nulls: bool
    result_order: Literal["explicit_top_n", "unordered_bounded"]
    timestamp_bucket_notes: tuple[str, ...]
    join_type: Literal["inner", "left"] | None = None
    left_join_preserves_unmatched_base_rows: bool = False
    lookup_filters_in_on: bool = False
    lookup_filters_in_where: bool = False


@dataclass(frozen=True)
class CompiledPostgresJoin:
    """Safe join provenance plus live uniqueness facts for the gateway."""

    relationship_id: UUID
    relationship_version_id: UUID
    version_number: int
    join_type: Literal["inner", "left"]
    lookup_asset_id: UUID
    lookup_schema_name: str
    lookup_table_name: str
    column_pairs: tuple[tuple[UUID, UUID], ...]
    # The approved catalog FK is revalidated remotely as an ordered mapping,
    # not inferred from compatible names or the lookup target key alone.
    foreign_key_constraint_name: str
    foreign_key_base_column_names: tuple[str, ...]
    foreign_key_lookup_column_names: tuple[str, ...]
    target_key_constraint_name: str
    target_key_kind: Literal["primary_key", "unique"]
    target_key_column_names: tuple[str, ...]


@dataclass(frozen=True)
class CompiledPostgresQuery:
    """Compiler output, not a public execution input or authorization token."""

    sql: str = field(repr=False)
    parameters: tuple[Any, ...] = field(repr=False)
    source_id: UUID
    snapshot_id: UUID
    asset_id: UUID
    expected_output_columns: tuple[ExpectedPostgresOutputColumn, ...]
    effective_limit: int
    ordering: tuple[CompiledPostgresOrdering, ...]
    semantics: PostgresCalculationSemantics
    fingerprint: str
    join: CompiledPostgresJoin | None = None
    # The transport accepts only a compiler-minted artifact.  This is an
    # internal fail-closed marker, not an authorization cache: Stage 4B still
    # recompiles against current owner/scope/head immediately before I/O.
    _seal: object | None = field(default=None, repr=False, compare=False, init=False)

    @property
    def is_explicit_top_n(self) -> bool:
        return bool(self.ordering)

    def safe_provenance(self) -> dict[str, object]:
        """Safe logging payload: no SQL text and never parameter values."""

        return {
            "fingerprint": self.fingerprint,
            "source_id": str(self.source_id),
            "snapshot_id": str(self.snapshot_id),
            "asset_id": str(self.asset_id),
            "relationship_id": str(self.join.relationship_id) if self.join is not None else None,
            "relationship_version_id": (
                str(self.join.relationship_version_id) if self.join is not None else None
            ),
            "join_type": self.join.join_type if self.join is not None else None,
            "parameter_count": len(self.parameters),
            "parameter_types": tuple(type(value).__name__ for value in self.parameters),
            "effective_limit": self.effective_limit,
            "ordering": tuple(
                {"alias": item.alias, "direction": item.direction}
                for item in self.ordering
            ),
            "output_aliases": tuple(column.alias for column in self.expected_output_columns),
        }

    def __repr__(self) -> str:
        return (
            "CompiledPostgresQuery("
            f"fingerprint={self.fingerprint!r}, source_id={self.source_id!r}, "
            f"snapshot_id={self.snapshot_id!r}, asset_id={self.asset_id!r}, "
            f"parameter_count={len(self.parameters)}, effective_limit={self.effective_limit})"
        )


@dataclass
class _ParameterBinder:
    values: list[Any] = field(default_factory=list)

    def bind(self, value: Any, postgres_type: str) -> str:
        self.values.append(value)
        return f"${len(self.values)}::{postgres_type}"


def _actual_native_type(column: CatalogColumn) -> str:
    schema = column.native_type_schema or "<unknown schema>"
    name = column.native_type_name or "<unknown type>"
    return f"{schema}.{name}"


def _resolve_supported_column(
    *,
    catalog_column: CatalogColumn,
    asset_id: UUID,
    field_name: str,
) -> ResolvedPostgresColumn:
    native_type_name = catalog_column.native_type_name
    if (
        catalog_column.native_type_schema != "pg_catalog"
        or native_type_name not in _SUPPORTED_NATIVE_TYPES
    ):
        raise PostgresQueryPlanValidationError(
            field=field_name,
            column_id=catalog_column.id,
            actual_type=_actual_native_type(catalog_column),
            reason="this PostgreSQL type is not supported by the query-plan language",
        )
    return ResolvedPostgresColumn(
        id=catalog_column.id,
        asset_id=asset_id,
        name=catalog_column.column_name,
        native_type_name=native_type_name,
        category=_CATEGORY_BY_NATIVE_TYPE[native_type_name],
        nullable=catalog_column.is_nullable,
    )


def _asset_id_for_reference(
    *,
    plan: PostgresQueryPlan,
    asset_id: UUID | None,
) -> UUID:
    """Return the base asset for legacy plans and the explicit one for joins."""

    return plan.asset_id if asset_id is None else asset_id


def _referenced_column_ids_by_asset(
    plan: PostgresQueryPlan,
    *,
    join_pairs: tuple[tuple[UUID, UUID], ...] = (),
) -> dict[UUID, tuple[UUID, ...]]:
    """Bound all catalog lookups by the two approved asset identities."""

    references: dict[UUID, list[UUID]] = {plan.asset_id: []}
    if plan.join is not None:
        references[plan.join.lookup_asset_id] = []

    def add(asset_id: UUID, column_id: UUID) -> None:
        if asset_id not in references:
            raise PostgresQueryPlanValidationError(
                field="join",
                reason="column references must use the declared base or lookup asset",
            )
        references[asset_id].append(column_id)

    for projection in plan.projections:
        add(_asset_id_for_reference(plan=plan, asset_id=projection.asset_id), projection.column_id)
    for filter_item in plan.filters:
        add(_asset_id_for_reference(plan=plan, asset_id=filter_item.asset_id), filter_item.column_id)
    for dimension in plan.group_by:
        add(_asset_id_for_reference(plan=plan, asset_id=dimension.asset_id), dimension.column_id)
    for aggregation in plan.aggregations:
        if aggregation.column_id is not None:
            add(
                _asset_id_for_reference(plan=plan, asset_id=aggregation.asset_id),
                aggregation.column_id,
            )
    for base_column_id, lookup_column_id in join_pairs:
        add(plan.asset_id, base_column_id)
        if plan.join is None:
            raise PostgresQueryPlanValidationError(
                field="join",
                reason="a relationship mapping requires an approved join",
            )
        add(plan.join.lookup_asset_id, lookup_column_id)
    return {
        asset_id: tuple(dict.fromkeys(column_ids))
        for asset_id, column_ids in references.items()
    }


def _validate_complexity(plan: PostgresQueryPlan, settings: Settings) -> None:
    checks = (
        ("projections", len(plan.projections), settings.postgres_query_max_projections),
        ("filters", len(plan.filters), settings.postgres_query_max_filters),
        ("aggregations", len(plan.aggregations), settings.postgres_query_max_aggregations),
        ("group_by", len(plan.group_by), settings.postgres_query_max_group_by),
    )
    for field_name, actual, maximum in checks:
        if actual > maximum:
            raise PostgresQueryPlanValidationError(
                field=field_name,
                reason=f"at most {maximum} {field_name} are allowed",
            )
    if plan.limit > settings.postgres_query_max_result_limit:
        raise PostgresQueryPlanValidationError(
            field="limit",
            reason=f"limit must not exceed {settings.postgres_query_max_result_limit}",
        )
    for index, filter_item in enumerate(plan.filters):
        if filter_item.values is not None and len(filter_item.values) > settings.postgres_query_max_in_values:
            raise PostgresQueryPlanValidationError(
                field=f"filters[{index}].values",
                reason=(
                    "membership values must not exceed "
                    f"{settings.postgres_query_max_in_values}"
                ),
                )


def _validate_join_structure(plan: PostgresQueryPlan) -> None:
    """Repeat the two-relation shape check at the compiler boundary.

    Normal callers reach this module through Pydantic validation.  This
    second check deliberately protects against an internal ``model_construct``
    or ``model_copy(update=...)`` bypass: an unqualified column must never
    quietly become a base-table reference in a joined statement.
    """

    join = plan.join
    references = (
        *(projection.asset_id for projection in plan.projections),
        *(filter_item.asset_id for filter_item in plan.filters),
        *(dimension.asset_id for dimension in plan.group_by),
        *(
            aggregation.asset_id
            for aggregation in plan.aggregations
            if aggregation.column_id is not None
        ),
    )
    if join is None:
        if any(asset_id is not None and asset_id != plan.asset_id for asset_id in references):
            raise PostgresQueryPlanValidationError(
                field="references",
                reason="single-table references must use the plan asset",
            )
        if any(filter_item.placement is not None for filter_item in plan.filters):
            raise PostgresQueryPlanValidationError(
                field="filters",
                reason="filter placement requires an approved join",
            )
        return

    if not isinstance(join, PostgresApprovedJoin):
        raise PostgresQueryPlanValidationError(
            field="join",
            reason="join must be an approved relationship reference",
        )
    if (
        not isinstance(join.relationship_id, UUID)
        or not isinstance(join.relationship_version_id, UUID)
        or not isinstance(join.lookup_asset_id, UUID)
        or join.join_type not in {"inner", "left"}
    ):
        raise PostgresQueryPlanValidationError(
            field="join",
            reason="approved relationship reference is malformed",
        )
    if join.lookup_asset_id == plan.asset_id:
        raise PostgresQueryPlanValidationError(
            field="join.lookup_asset_id",
            reason="an approved join requires distinct base and lookup assets",
        )
    allowed_asset_ids = {plan.asset_id, join.lookup_asset_id}
    if any(asset_id is None for asset_id in references) or any(
        asset_id not in allowed_asset_ids for asset_id in references
    ):
        raise PostgresQueryPlanValidationError(
            field="references",
            reason="join plans require qualified base or lookup asset references",
        )
    for index, filter_item in enumerate(plan.filters):
        if filter_item.asset_id == plan.asset_id and filter_item.placement == "join_on":
            raise PostgresQueryPlanValidationError(
                field=f"filters[{index}].placement",
                reason="base-table filters cannot use join_on placement",
            )
        if (
            join.join_type == "left"
            and filter_item.asset_id == join.lookup_asset_id
            and filter_item.placement not in {"where", "join_on"}
        ):
            raise PostgresQueryPlanValidationError(
                field=f"filters[{index}].placement",
                reason="LEFT JOIN lookup filters require explicit placement",
            )


def _column_for(
    columns: Mapping[UUID, ResolvedPostgresColumn],
    column_id: UUID,
    field_name: str,
    *,
    asset_id: UUID | None = None,
) -> ResolvedPostgresColumn:
    column = columns.get(column_id)
    if column is None:
        # The catalog resolver should already make this impossible. Retain the
        # check so a future internal caller cannot compile an unscoped record.
        raise PostgresQueryPlanValidationError(
            field=field_name,
            column_id=column_id,
            reason="column is not available in the authorized catalog asset",
        )
    if asset_id is not None and column.asset_id != asset_id:
        raise PostgresQueryPlanValidationError(
            field=field_name,
            column_id=column_id,
            reason="column is not available in the qualified catalog asset",
        )
    return column


def _require_supported_filter_operator(
    filter_item: PostgresFilter,
    column: ResolvedPostgresColumn,
    field_name: str,
) -> None:
    if filter_item.operator not in _FILTER_OPERATORS_BY_CATEGORY[column.category]:
        raise PostgresQueryPlanValidationError(
            field=field_name,
            column_id=column.id,
            actual_type=column.native_type_name,
            reason=f"{filter_item.operator} is not supported for this column type",
        )


def _value_for_column(
    value: TypedPostgresValue,
    column: ResolvedPostgresColumn,
    field_name: str,
) -> Any:
    """Validate an explicit typed literal and return its asyncpg value."""

    # Keep explicit isinstance branches rather than a dynamic type map: this
    # security boundary needs bool-vs-int and timestamp timezone distinctions.
    expected_kind = "compatible"
    if column.category == "integer":
        if not isinstance(value, IntegerValue):
            expected_kind = "integer"
        else:
            lower, upper = _INTEGER_TYPE_RANGES[column.native_type_name]
            if not lower <= value.value <= upper:
                raise PostgresQueryPlanValidationError(
                    field=field_name,
                    column_id=column.id,
                    actual_type=column.native_type_name,
                    reason="integer value is outside this PostgreSQL type's range",
                )
            return value.value
    elif column.category == "decimal":
        if isinstance(value, DecimalValue):
            return value.value
        expected_kind = "decimal"
    elif column.category == "number":
        if isinstance(value, NumberValue):
            return value.value
        expected_kind = "number"
    elif column.category == "boolean":
        if isinstance(value, BooleanValue):
            return value.value
        expected_kind = "boolean"
    elif column.category == "text":
        if isinstance(value, TextValue):
            return value.value
        expected_kind = "text"
    elif column.category == "uuid":
        if isinstance(value, UUIDValue):
            return value.value
        expected_kind = "uuid"
    elif column.category == "date":
        if isinstance(value, DateValue):
            return value.value
        expected_kind = "date"
    elif column.category == "timestamp":
        if isinstance(value, TimestampValue):
            return value.value
        expected_kind = "timestamp"
    elif column.category == "timestamptz":
        if isinstance(value, TimestamptzValue):
            return value.value
        expected_kind = "timestamptz"
    raise PostgresQueryPlanValidationError(
        field=field_name,
        column_id=column.id,
        actual_type=column.native_type_name,
        reason=f"this column requires a {expected_kind} typed value",
    )


def _validate_filter(
    filter_item: PostgresFilter,
    *,
    column: ResolvedPostgresColumn,
    index: int,
) -> None:
    field_name = f"filters[{index}]"
    _require_supported_filter_operator(filter_item, column, field_name)
    if filter_item.value is not None:
        _value_for_column(filter_item.value, column, f"{field_name}.value")
    if filter_item.values is not None:
        for value_index, value in enumerate(filter_item.values):
            _value_for_column(value, column, f"{field_name}.values[{value_index}]")


def _validate_aggregation(
    aggregation: PostgresAggregation,
    *,
    columns: Mapping[UUID, ResolvedPostgresColumn],
    index: int,
    asset_id: UUID | None = None,
) -> None:
    if aggregation.operation == "count_all":
        return
    assert aggregation.column_id is not None  # guaranteed by strict plan model
    column = _column_for(
        columns,
        aggregation.column_id,
        f"aggregations[{index}].column_id",
        asset_id=asset_id,
    )
    if aggregation.operation == "count_column":
        return
    if aggregation.operation in {"sum", "average"} and column.category not in {
        "integer",
        "decimal",
        "number",
    }:
        raise PostgresQueryPlanValidationError(
            field=f"aggregations[{index}]",
            column_id=column.id,
            actual_type=column.native_type_name,
            reason=f"{aggregation.operation} requires a numeric column",
        )
    if aggregation.operation in {"minimum", "maximum"} and column.category not in {
        "integer",
        "decimal",
        "number",
        "date",
        "timestamp",
        "timestamptz",
    }:
        raise PostgresQueryPlanValidationError(
            field=f"aggregations[{index}]",
            column_id=column.id,
            actual_type=column.native_type_name,
            reason=f"{aggregation.operation} is not supported for this column type",
        )


def _validate_group_dimension(
    dimension: PostgresGroupDimension,
    *,
    columns: Mapping[UUID, ResolvedPostgresColumn],
    index: int,
    asset_id: UUID | None = None,
) -> None:
    column = _column_for(
        columns,
        dimension.column_id,
        f"group_by[{index}].column_id",
        asset_id=asset_id,
    )
    if dimension.bucket is None:
        return
    allowed_buckets = _BUCKETS_BY_CATEGORY.get(column.category, frozenset())
    if dimension.bucket not in allowed_buckets:
        raise PostgresQueryPlanValidationError(
            field=f"group_by[{index}].bucket",
            column_id=column.id,
            actual_type=column.native_type_name,
            reason="this time bucket is not supported for the column type",
        )
    if column.category == "timestamptz" and dimension.time_zone is None:
        raise PostgresQueryPlanValidationError(
            field=f"group_by[{index}].time_zone",
            column_id=column.id,
            actual_type=column.native_type_name,
            reason="timestamptz buckets require an explicit IANA timezone",
        )
    if column.category != "timestamptz" and dimension.time_zone is not None:
        raise PostgresQueryPlanValidationError(
            field=f"group_by[{index}].time_zone",
            column_id=column.id,
            actual_type=column.native_type_name,
            reason="time_zone is only supported for timestamptz buckets",
        )


def _validate_sort(plan: PostgresQueryPlan) -> None:
    if plan.sort is None:
        return
    if plan.aggregations and plan.sort.output_kind == "projection":
        raise PostgresQueryPlanValidationError(
            field="sort.output_kind",
            reason="aggregate plans cannot sort a raw projection",
        )
    if not plan.aggregations and plan.sort.output_kind != "projection":
        raise PostgresQueryPlanValidationError(
            field="sort.output_kind",
            reason="non-aggregate plans can sort only a projection",
        )
    output_count_by_kind = {
        "projection": len(plan.projections),
        "grouping": len(plan.group_by),
        "aggregation": len(plan.aggregations),
    }
    output_count = output_count_by_kind[plan.sort.output_kind]
    if plan.sort.output_index >= output_count:
        raise PostgresQueryPlanValidationError(
            field="sort.output_index",
            reason="sort must reference a selected compiler output",
        )


def _normalized_join_type(value: object) -> Literal["inner", "left"]:
    raw_value = getattr(value, "value", value)
    if not isinstance(raw_value, str) or raw_value.lower() not in {"inner", "left"}:
        raise PostgresQueryPlanValidationError(
            field="join.join_type",
            reason="approved relationship has an unsupported join type",
        )
    return raw_value.lower()  # type: ignore[return-value]


def _normalized_target_key_kind(value: object) -> Literal["primary_key", "unique"]:
    raw_value = getattr(value, "value", value)
    if not isinstance(raw_value, str):
        raise PostgresQueryPlanValidationError(
            field="join.target_key",
            reason="approved relationship has unsupported target-key evidence",
        )
    normalized = raw_value.lower()
    if normalized == "primary_key":
        return "primary_key"
    if normalized == "unique":
        return "unique"
    raise PostgresQueryPlanValidationError(
        field="join.target_key",
        reason="approved relationship has unsupported target-key evidence",
    )


async def _resolve_approved_join_for_plan(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    source_id: UUID,
    snapshot_id: UUID,
    plan: PostgresQueryPlan,
) -> object | None:
    """Resolve a plan's ID-only relationship through the trusted service.

    Keeping this import local avoids a model/service cycle while preserving a
    hard boundary: a model-produced UUID is never a SQL capability.  The
    relationship service rechecks owner, source head, scope, lifecycle, and
    immutable version before returning structural facts.
    """

    if plan.join is None:
        return None
    try:
        from app.services.relationship_service import resolve_relationship_definition_for_owner
    except ImportError as exc:  # pragma: no cover - deployment wiring error
        raise PostgresQueryPlanValidationError(
            field="join",
            reason="approved relationship support is unavailable",
        ) from exc
    resolved = await resolve_relationship_definition_for_owner(
        session,
        owner_user_id=owner_user_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        relationship_id=plan.join.relationship_id,
        relationship_version_id=plan.join.relationship_version_id,
        require_current=True,
    )
    if (
        getattr(resolved, "relationship_id", None) != plan.join.relationship_id
        or getattr(resolved, "relationship_version_id", None)
        != plan.join.relationship_version_id
        or getattr(resolved, "base_asset_id", None) != plan.asset_id
        or getattr(resolved, "lookup_asset_id", None) != plan.join.lookup_asset_id
        or _normalized_join_type(getattr(resolved, "join_type", None)) != plan.join.join_type
    ):
        raise PostgresQueryPlanValidationError(
            field="join",
            reason="approved relationship does not match the typed join plan",
        )
    return resolved


def _relationship_pair_ids(resolved_relationship: object) -> tuple[tuple[UUID, UUID], ...]:
    pairs = getattr(resolved_relationship, "column_pairs", None)
    if not isinstance(pairs, tuple) or not pairs:
        raise PostgresQueryPlanValidationError(
            field="join.column_pairs",
            reason="approved relationship has no ordered key mapping",
        )
    result: list[tuple[UUID, UUID]] = []
    for pair in pairs:
        base_column_id = getattr(pair, "base_column_id", None)
        lookup_column_id = getattr(pair, "lookup_column_id", None)
        if not isinstance(base_column_id, UUID) or not isinstance(lookup_column_id, UUID):
            raise PostgresQueryPlanValidationError(
                field="join.column_pairs",
                reason="approved relationship has an invalid key mapping",
            )
        result.append((base_column_id, lookup_column_id))
    if len(result) != len(set(result)) or len({item[0] for item in result}) != len(result) or len(
        {item[1] for item in result}
    ) != len(result):
        raise PostgresQueryPlanValidationError(
            field="join.column_pairs",
            reason="approved relationship has duplicate key mappings",
        )
    return tuple(result)


def _materialize_resolved_join(
    *,
    plan: PostgresQueryPlan,
    relationship: object,
    columns: Mapping[UUID, ResolvedPostgresColumn],
) -> ResolvedPostgresJoin:
    """Cross-check service facts against current catalog records for SQL use."""

    assert plan.join is not None
    pair_ids = _relationship_pair_ids(relationship)
    materialized_pairs: list[ResolvedPostgresJoinColumnPair] = []
    for index, (base_column_id, lookup_column_id) in enumerate(pair_ids):
        base_column = _column_for(
            columns,
            base_column_id,
            f"join.column_pairs[{index}].base_column_id",
            asset_id=plan.asset_id,
        )
        lookup_column = _column_for(
            columns,
            lookup_column_id,
            f"join.column_pairs[{index}].lookup_column_id",
            asset_id=plan.join.lookup_asset_id,
        )
        # PostgreSQL permits some implicit FK-compatible pairs. This first
        # bounded implementation intentionally requires the same supported
        # pg_catalog type so generated equality has no hidden coercion or
        # collation/operator-class ambiguity.
        if base_column.native_type_name != lookup_column.native_type_name:
            raise PostgresQueryPlanValidationError(
                field=f"join.column_pairs[{index}]",
                reason="join key columns must have the same supported PostgreSQL type",
            )
        materialized_pairs.append(
            ResolvedPostgresJoinColumnPair(
                base_column=base_column,
                lookup_column=lookup_column,
            )
        )

    target_key = getattr(relationship, "target_key", None)
    foreign_key_constraint_name = getattr(relationship, "foreign_key_constraint_name", None)
    target_key_column_ids = getattr(target_key, "lookup_column_ids", None)
    target_key_column_names = getattr(target_key, "lookup_column_names", None)
    target_key_constraint_name = getattr(target_key, "constraint_name", None)
    target_key_kind = _normalized_target_key_kind(getattr(target_key, "key_kind", None))
    if (
        not isinstance(foreign_key_constraint_name, str)
        or not foreign_key_constraint_name
        or not isinstance(target_key_column_ids, tuple)
        or not isinstance(target_key_column_names, tuple)
        or not isinstance(target_key_constraint_name, str)
        or not target_key_constraint_name
        or tuple(target_key_column_ids) != tuple(item[1] for item in pair_ids)
        or tuple(target_key_column_names)
        != tuple(item.lookup_column.name for item in materialized_pairs)
    ):
        raise PostgresQueryPlanValidationError(
            field="join.target_key",
            reason="approved relationship target key does not match its ordered lookup mapping",
        )

    version_number = getattr(relationship, "version_number", None)
    lookup_schema_name = getattr(relationship, "lookup_schema_name", None)
    lookup_table_name = getattr(relationship, "lookup_table_name", None)
    if (
        not isinstance(version_number, int)
        or version_number < 1
        or not isinstance(lookup_schema_name, str)
        or not isinstance(lookup_table_name, str)
    ):
        raise PostgresQueryPlanValidationError(
            field="join",
            reason="approved relationship has incomplete current catalog facts",
        )
    return ResolvedPostgresJoin(
        relationship_id=plan.join.relationship_id,
        relationship_version_id=plan.join.relationship_version_id,
        version_number=version_number,
        join_type=plan.join.join_type,
        lookup_asset_id=plan.join.lookup_asset_id,
        lookup_schema_name=lookup_schema_name,
        lookup_table_name=lookup_table_name,
        column_pairs=tuple(materialized_pairs),
        foreign_key_constraint_name=foreign_key_constraint_name,
        target_key_constraint_name=target_key_constraint_name,
        target_key_kind=target_key_kind,
        target_key_column_names=tuple(target_key_column_names),
    )


async def validate_postgres_query_plan(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    source_id: UUID,
    snapshot_id: UUID,
    plan: PostgresQueryPlan,
    settings: Settings,
) -> ValidatedPostgresQueryPlan:
    """Bind an untrusted structural plan to the current authorized catalog.

    ``owner_user_id``, ``source_id``, and ``snapshot_id`` must originate from
    the authenticated application flow. This function deliberately does not
    accept an API key, optional actor, or source/snapshot supplied by the plan.
    Historical snapshots are rejected instead of silently being retargeted.
    """

    if not isinstance(plan, PostgresQueryPlan):
        raise TypeError("plan must be a PostgresQueryPlan")
    _validate_join_structure(plan)
    _validate_complexity(plan, settings)
    resolved_relationship = await _resolve_approved_join_for_plan(
        session,
        owner_user_id=owner_user_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        plan=plan,
    )
    relationship_pair_ids = (
        _relationship_pair_ids(resolved_relationship)
        if resolved_relationship is not None
        else ()
    )
    references_by_asset = _referenced_column_ids_by_asset(
        plan,
        join_pairs=relationship_pair_ids,
    )
    context = await resolve_postgres_query_catalog_context_for_owner(
        session,
        source_id=source_id,
        snapshot_id=snapshot_id,
        asset_id=plan.asset_id,
        column_ids=references_by_asset[plan.asset_id],
        owner_user_id=owner_user_id,
    )
    contexts = [context]
    if plan.join is not None:
        lookup_context = await resolve_postgres_query_catalog_context_for_owner(
            session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=plan.join.lookup_asset_id,
            column_ids=references_by_asset[plan.join.lookup_asset_id],
            owner_user_id=owner_user_id,
        )
        if (
            lookup_context.source.id != context.source.id
            or lookup_context.snapshot.id != context.snapshot.id
            or lookup_context.current_scope_fingerprint != context.current_scope_fingerprint
        ):
            raise PostgresQueryPlanValidationError(
                field="join",
                reason="both join tables must belong to one current source and catalog snapshot",
            )
        contexts.append(lookup_context)
    columns = {
        column_id: _resolve_supported_column(
            catalog_column=catalog_column,
            asset_id=resolved_context.asset.id,
            field_name="catalog.columns",
        )
        for resolved_context in contexts
        for column_id, catalog_column in resolved_context.columns.items()
    }
    materialized_join = (
        _materialize_resolved_join(
            plan=plan,
            relationship=resolved_relationship,
            columns=columns,
        )
        if resolved_relationship is not None
        else None
    )
    _validate_semantics(plan, columns)
    _validate_parameter_budget(plan, columns, settings)
    return ValidatedPostgresQueryPlan(
        source_id=context.source.id,
        snapshot_id=context.snapshot.id,
        asset_id=context.asset.id,
        schema_name=context.asset.schema_name,
        table_name=context.asset.table_name,
        current_scope_fingerprint=context.current_scope_fingerprint,
        columns=MappingProxyType(columns),
        join=materialized_join,
        plan=plan,
        _seal=_VALIDATED_PLAN_SEAL,
    )


def _validate_semantics(
    plan: PostgresQueryPlan,
    columns: Mapping[UUID, ResolvedPostgresColumn],
) -> None:
    for index, projection in enumerate(plan.projections):
        _column_for(
            columns,
            projection.column_id,
            f"projections[{index}].column_id",
            asset_id=_asset_id_for_reference(plan=plan, asset_id=projection.asset_id),
        )
    for index, filter_item in enumerate(plan.filters):
        column = _column_for(
            columns,
            filter_item.column_id,
            f"filters[{index}].column_id",
            asset_id=_asset_id_for_reference(plan=plan, asset_id=filter_item.asset_id),
        )
        _validate_filter(filter_item, column=column, index=index)
    for index, dimension in enumerate(plan.group_by):
        _validate_group_dimension(
            dimension,
            columns=columns,
            index=index,
            asset_id=_asset_id_for_reference(plan=plan, asset_id=dimension.asset_id),
        )
    for index, aggregation in enumerate(plan.aggregations):
        # A confirmed metric's declared aggregation grain remains the base
        # asset even when a lookup supplies dimensions or explicit filters.
        # This compiler-level check protects the generic execution entrypoint
        # as well as the metric expander.
        if (
            plan.join is not None
            and aggregation.operation != "count_all"
            and _asset_id_for_reference(plan=plan, asset_id=aggregation.asset_id)
            != plan.asset_id
        ):
            raise PostgresQueryPlanValidationError(
                field=f"aggregations[{index}].asset_id",
                reason="joined calculations must remain on the base asset",
            )
        _validate_aggregation(
            aggregation,
            columns=columns,
            index=index,
            asset_id=(
                _asset_id_for_reference(plan=plan, asset_id=aggregation.asset_id)
                if aggregation.column_id is not None
                else None
            ),
        )
    _validate_sort(plan)


def _parameter_byte_length(value: Any) -> int:
    """Conservative estimate for the known asyncpg parameter value shapes."""

    if isinstance(value, list):
        # Include delimiter/array-metadata headroom rather than pretending a
        # text representation exactly matches asyncpg's binary wire encoding.
        return 16 + sum(_parameter_byte_length(item) + 1 for item in value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, UUID):
        return 16
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return 8
    if isinstance(value, float):
        return 8
    if isinstance(value, Decimal):
        return len(str(value).encode("ascii"))
    if isinstance(value, (date, datetime)):
        return len(value.isoformat().encode("ascii"))
    raise TypeError(f"unsupported validated parameter type: {type(value).__name__}")


def _validate_parameter_budget(
    plan: PostgresQueryPlan,
    columns: Mapping[UUID, ResolvedPostgresColumn],
    settings: Settings,
) -> None:
    """Reject oversized input before a future executor can serialize it."""

    values: list[Any] = [plan.limit]
    for filter_item in plan.filters:
        column = _column_for(
            columns,
            filter_item.column_id,
            "filters.column_id",
            asset_id=_asset_id_for_reference(plan=plan, asset_id=filter_item.asset_id),
        )
        if filter_item.value is not None:
            values.append(_value_for_column(filter_item.value, column, "filters.value"))
        elif filter_item.values is not None:
            values.append(
                [
                    _value_for_column(value, column, "filters.values")
                    for value in filter_item.values
                ]
            )
    for dimension in plan.group_by:
        if dimension.time_zone is not None:
            values.append(dimension.time_zone)
    estimated_bytes = sum(_parameter_byte_length(value) for value in values)
    if estimated_bytes > settings.postgres_query_max_parameter_bytes:
        raise PostgresQueryPlanValidationError(
            field="parameters",
            reason=(
                "bound parameter data must not exceed "
                f"{settings.postgres_query_max_parameter_bytes} bytes"
            ),
        )


def _quote_identifier(identifier: str) -> str:
    """Quote one server-resolved identifier, never an untrusted dotted path."""

    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise PostgresQueryPlanValidationError(
            field="catalog.identifier",
            reason="catalog identifier is not safe to compile",
        )
    return '"' + identifier.replace('"', '""') + '"'


def _column_sql(
    column: ResolvedPostgresColumn,
    *,
    base_asset_id: UUID,
    joined: bool,
) -> str:
    """Render only compiler-owned aliases for a current catalog column."""

    if not joined:
        alias = "t"
    elif column.asset_id == base_asset_id:
        alias = "b"
    else:
        alias = "l"
    return f"{_quote_identifier(alias)}.{_quote_identifier(column.name)}"


def _output_alias(kind: Literal["projection", "grouping", "aggregation"], index: int) -> str:
    prefix = {"projection": "p", "grouping": "g", "aggregation": "a"}[kind]
    return f"__nv_{prefix}{index}"


def _postgres_type_for(column: ResolvedPostgresColumn) -> str:
    return _SQL_TYPE_BY_NATIVE_TYPE[column.native_type_name]


def _compile_filter(
    filter_item: PostgresFilter,
    *,
    column: ResolvedPostgresColumn,
    binder: _ParameterBinder,
    base_asset_id: UUID,
    joined: bool,
) -> str:
    column_sql = _column_sql(column, base_asset_id=base_asset_id, joined=joined)
    operator = filter_item.operator
    if operator == "is_null":
        return f"{column_sql} IS NULL"
    if operator == "is_not_null":
        return f"{column_sql} IS NOT NULL"
    if operator in {"contains", "not_contains"}:
        assert filter_item.value is not None
        parameter = binder.bind(
            _value_for_column(filter_item.value, column, "filters.value"),
            "pg_catalog.text",
        )
        comparison = "> 0" if operator == "contains" else "= 0"
        return f"pg_catalog.strpos({column_sql}, {parameter}) {comparison}"
    if operator in {"in", "not_in"}:
        assert filter_item.values is not None
        values = [
            _value_for_column(value, column, "filters.values")
            for value in filter_item.values
        ]
        parameter = binder.bind(values, f"{_postgres_type_for(column)}[]")
        membership_operator = "= ANY" if operator == "in" else "<> ALL"
        return f"{column_sql} {membership_operator}({parameter})"

    assert filter_item.value is not None
    parameter = binder.bind(
        _value_for_column(filter_item.value, column, "filters.value"),
        _postgres_type_for(column),
    )
    sql_operator = {
        "eq": "=",
        "neq": "<>",
        "lt": "<",
        "lte": "<=",
        "gt": ">",
        "gte": ">=",
    }[operator]
    return f"{column_sql} {sql_operator} {parameter}"


def _compile_group_expression(
    dimension: PostgresGroupDimension,
    *,
    column: ResolvedPostgresColumn,
    binder: _ParameterBinder,
    base_asset_id: UUID,
    joined: bool,
) -> tuple[str, str, str | None]:
    """Return SQL expression, expected PostgreSQL type, and timezone semantics."""

    column_sql = _column_sql(column, base_asset_id=base_asset_id, joined=joined)
    if dimension.bucket is None:
        return column_sql, column.native_type_name, None

    bucket = dimension.bucket
    if column.category == "date":
        if bucket == "day":
            return column_sql, "date", "date-only calendar day; no timezone conversion"
        return (
            f"pg_catalog.date_trunc('{bucket}', {column_sql}::pg_catalog.timestamp)::pg_catalog.date",
            "date",
            "date-only calendar bucket; no timezone conversion",
        )
    if column.category == "timestamp":
        return (
            f"pg_catalog.date_trunc('{bucket}', {column_sql})",
            "timestamp",
            "timestamp without time zone; no timezone conversion",
        )
    assert column.category == "timestamptz"
    assert dimension.time_zone is not None
    timezone_parameter = binder.bind(dimension.time_zone, "pg_catalog.text")
    return (
        f"pg_catalog.date_trunc('{bucket}', pg_catalog.timezone({timezone_parameter}, {column_sql}))",
        "timestamp",
        f"timestamptz bucket rendered as local timestamp in {dimension.time_zone}",
    )


def _aggregate_postgres_type(aggregation: PostgresAggregation, column: ResolvedPostgresColumn | None) -> str:
    if aggregation.operation in {"count_all", "count_column"}:
        return "int8"
    assert column is not None
    if aggregation.operation == "sum":
        if column.native_type_name in {"int2", "int4"}:
            return "int8"
        if column.native_type_name in {"int8", "numeric"}:
            return "numeric"
        return "float8"
    if aggregation.operation == "average":
        if column.native_type_name in {"float4", "float8"}:
            return "float8"
        return "numeric"
    return column.native_type_name


def _compile_aggregation(
    aggregation: PostgresAggregation,
    *,
    columns: Mapping[UUID, ResolvedPostgresColumn],
    base_asset_id: UUID,
    joined: bool,
    asset_id: UUID | None = None,
) -> tuple[str, ResolvedPostgresColumn | None, str]:
    if aggregation.operation == "count_all":
        return "pg_catalog.count(*)", None, "int8"
    assert aggregation.column_id is not None
    column = _column_for(
        columns,
        aggregation.column_id,
        "aggregations.column_id",
        asset_id=asset_id,
    )
    column_sql = _column_sql(column, base_asset_id=base_asset_id, joined=joined)
    function_by_operation = {
        "count_column": "count",
        "sum": "sum",
        "average": "avg",
        "minimum": "min",
        "maximum": "max",
    }
    function_name = function_by_operation[aggregation.operation]
    return (
        f"pg_catalog.{function_name}({column_sql})",
        column,
        _aggregate_postgres_type(aggregation, column),
    )


def _compile_fingerprint(
    validated: ValidatedPostgresQueryPlan,
) -> str:
    """Hash only structural metadata; low-entropy parameter values stay absent."""

    plan = validated.plan
    payload = {
        "language": "postgres_query_plan_v1",
        "source_id": str(validated.source_id),
        "snapshot_id": str(validated.snapshot_id),
        "asset_id": str(validated.asset_id),
        "scope_fingerprint": validated.current_scope_fingerprint,
        "join": (
            {
                "relationship_id": str(validated.join.relationship_id),
                "relationship_version_id": str(validated.join.relationship_version_id),
                "version_number": validated.join.version_number,
                "join_type": validated.join.join_type,
                "lookup_asset_id": str(validated.join.lookup_asset_id),
                "foreign_key_constraint_name": validated.join.foreign_key_constraint_name,
                "foreign_key_base_column_names": [
                    pair.base_column.name for pair in validated.join.column_pairs
                ],
                "foreign_key_lookup_column_names": [
                    pair.lookup_column.name for pair in validated.join.column_pairs
                ],
                "column_pairs": [
                    {
                        "base_column_id": str(pair.base_column.id),
                        "lookup_column_id": str(pair.lookup_column.id),
                    }
                    for pair in validated.join.column_pairs
                ],
                "target_key_constraint_name": validated.join.target_key_constraint_name,
                "target_key_kind": validated.join.target_key_kind,
                "target_key_column_names": list(validated.join.target_key_column_names),
            }
            if validated.join is not None
            else None
        ),
        "projections": [
            {"asset_id": str(_asset_id_for_reference(plan=plan, asset_id=item.asset_id)), "column_id": str(item.column_id)}
            for item in plan.projections
        ],
        "filters": [
            {
                "asset_id": str(_asset_id_for_reference(plan=plan, asset_id=item.asset_id)),
                "column_id": str(item.column_id),
                "operator": item.operator,
                "placement": item.placement,
                "value_kind": item.value.kind if item.value is not None else None,
                "value_kinds": [value.kind for value in item.values] if item.values else [],
            }
            for item in plan.filters
        ],
        "aggregations": [
            {
                "operation": item.operation,
                "asset_id": (
                    str(_asset_id_for_reference(plan=plan, asset_id=item.asset_id))
                    if item.column_id is not None
                    else None
                ),
                "column_id": str(item.column_id) if item.column_id else None,
            }
            for item in plan.aggregations
        ],
        "group_by": [
            {
                "asset_id": str(_asset_id_for_reference(plan=plan, asset_id=item.asset_id)),
                "column_id": str(item.column_id),
                "bucket": item.bucket,
                # An IANA timezone is structural (not a sensitive filter
                # value) and changes the generated result semantics.
                "time_zone": item.time_zone,
            }
            for item in plan.group_by
        ],
        "sort": (
            {
                "output_kind": plan.sort.output_kind,
                "output_index": plan.sort.output_index,
                "direction": plan.sort.direction,
            }
            if plan.sort is not None
            else None
        ),
        "limit": plan.limit,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compile_validated_postgres_query(
    validated: ValidatedPostgresQueryPlan,
) -> CompiledPostgresQuery:
    """Compile one previously authorized and semantically validated plan.

    This function accepts the resolved wrapper rather than a raw plan so it
    cannot turn catalog display names, source IDs, or untrusted model content
    into SQL. It is pure: it performs neither I/O nor query execution.
    """

    if not isinstance(validated, ValidatedPostgresQueryPlan):
        raise TypeError("validated must be a ValidatedPostgresQueryPlan")
    if validated._seal is not _VALIDATED_PLAN_SEAL:
        raise ValueError(
            "validated PostgreSQL plan was not created by the authorization boundary"
        )
    plan = validated.plan
    joined = validated.join is not None
    binder = _ParameterBinder()
    select_items: list[str] = []
    outputs: list[ExpectedPostgresOutputColumn] = []
    group_positions: list[str] = []
    bucket_notes: list[str] = []

    if plan.aggregations:
        for index, dimension in enumerate(plan.group_by):
            column = _column_for(
                validated.columns,
                dimension.column_id,
                f"group_by[{index}].column_id",
                asset_id=_asset_id_for_reference(plan=plan, asset_id=dimension.asset_id),
            )
            expression, postgres_type, note = _compile_group_expression(
                dimension,
                column=column,
                binder=binder,
                base_asset_id=validated.asset_id,
                joined=joined,
            )
            alias = _output_alias("grouping", index)
            select_items.append(f"{expression} AS {_quote_identifier(alias)}")
            group_positions.append(str(index + 1))
            outputs.append(
                ExpectedPostgresOutputColumn(
                    alias=alias,
                    logical_type="date_bucket" if dimension.bucket is not None else column.category,
                    postgres_type=postgres_type,
                    source_column_id=column.id,
                    source_column_name=column.name,
                    source_asset_id=column.asset_id,
                    bucket=dimension.bucket,
                    time_zone=dimension.time_zone,
                )
            )
            if note is not None:
                bucket_notes.append(note)
        for index, aggregation in enumerate(plan.aggregations):
            expression, column, postgres_type = _compile_aggregation(
                aggregation,
                columns=validated.columns,
                base_asset_id=validated.asset_id,
                joined=joined,
                asset_id=(
                    _asset_id_for_reference(plan=plan, asset_id=aggregation.asset_id)
                    if aggregation.column_id is not None
                    else None
                ),
            )
            alias = _output_alias("aggregation", index)
            select_items.append(f"{expression} AS {_quote_identifier(alias)}")
            outputs.append(
                ExpectedPostgresOutputColumn(
                    alias=alias,
                    logical_type="count" if aggregation.operation.startswith("count") else "aggregate",
                    postgres_type=postgres_type,
                    source_column_id=column.id if column is not None else None,
                    source_column_name=column.name if column is not None else None,
                    source_asset_id=column.asset_id if column is not None else None,
                    operation=aggregation.operation,
                )
            )
    else:
        for index, projection in enumerate(plan.projections):
            column = _column_for(
                validated.columns,
                projection.column_id,
                f"projections[{index}].column_id",
                asset_id=_asset_id_for_reference(plan=plan, asset_id=projection.asset_id),
            )
            alias = _output_alias("projection", index)
            select_items.append(
                f"{_column_sql(column, base_asset_id=validated.asset_id, joined=joined)} "
                f"AS {_quote_identifier(alias)}"
            )
            outputs.append(
                ExpectedPostgresOutputColumn(
                    alias=alias,
                    logical_type=column.category,
                    postgres_type=column.native_type_name,
                    source_column_id=column.id,
                    source_column_name=column.name,
                    source_asset_id=column.asset_id,
                )
            )

    where_items: list[str] = []
    join_on_filter_items: list[str] = []
    lookup_where_filters: list[PostgresFilter] = []
    for index, filter_item in enumerate(plan.filters):
        filter_asset_id = _asset_id_for_reference(plan=plan, asset_id=filter_item.asset_id)
        expression = _compile_filter(
            filter_item,
            column=_column_for(
                validated.columns,
                filter_item.column_id,
                f"filters[{index}].column_id",
                asset_id=filter_asset_id,
            ),
            binder=binder,
            base_asset_id=validated.asset_id,
            joined=joined,
        )
        if (
            validated.join is not None
            and filter_asset_id == validated.join.lookup_asset_id
            and filter_item.placement == "join_on"
        ):
            join_on_filter_items.append(expression)
        else:
            where_items.append(expression)
            if (
                validated.join is not None
                and filter_asset_id == validated.join.lookup_asset_id
            ):
                lookup_where_filters.append(filter_item)

    base_alias = "b" if joined else "t"
    table_sql = (
        f"{_quote_identifier(validated.schema_name)}."
        f"{_quote_identifier(validated.table_name)} AS {_quote_identifier(base_alias)}"
    )
    if validated.join is not None:
        equality_items = [
            (
                f"{_column_sql(pair.base_column, base_asset_id=validated.asset_id, joined=True)} "
                f"= {_column_sql(pair.lookup_column, base_asset_id=validated.asset_id, joined=True)}"
            )
            for pair in validated.join.column_pairs
        ]
        on_items = [*equality_items, *join_on_filter_items]
        lookup_sql = (
            f"{_quote_identifier(validated.join.lookup_schema_name)}."
            f"{_quote_identifier(validated.join.lookup_table_name)} AS {_quote_identifier('l')}"
        )
        table_sql = (
            f"{table_sql}\n{validated.join.join_type.upper()} JOIN {lookup_sql} ON "
            + " AND ".join(f"({item})" for item in on_items)
        )
    sql_parts = ["SELECT " + ", ".join(select_items), "FROM " + table_sql]
    if where_items:
        sql_parts.append("WHERE " + " AND ".join(f"({item})" for item in where_items))
    if group_positions:
        sql_parts.append("GROUP BY " + ", ".join(group_positions))

    ordering: tuple[CompiledPostgresOrdering, ...] = ()
    if plan.sort is not None:
        alias = _output_alias(plan.sort.output_kind, plan.sort.output_index)
        direction = plan.sort.direction.upper()
        sql_parts.append(f"ORDER BY {_quote_identifier(alias)} {direction}")
        ordering = (CompiledPostgresOrdering(alias=alias, direction=plan.sort.direction),)

    limit_parameter = binder.bind(plan.limit, "pg_catalog.int8")
    sql_parts.append(f"LIMIT {limit_parameter}")
    semantics = PostgresCalculationSemantics(
        filters_apply_before_aggregation=True,
        post_aggregation_filters_supported=False,
        count_all_is_row_count=any(item.operation == "count_all" for item in plan.aggregations),
        count_column_excludes_nulls=any(item.operation == "count_column" for item in plan.aggregations),
        result_order="explicit_top_n" if ordering else "unordered_bounded",
        timestamp_bucket_notes=tuple(bucket_notes),
        join_type=validated.join.join_type if validated.join is not None else None,
        left_join_preserves_unmatched_base_rows=(
            validated.join is not None
            and validated.join.join_type == "left"
            # An unmatched lookup is all NULL. In WHERE, only an explicit
            # ``IS NULL`` lookup predicate remains true for that row; every
            # other currently supported predicate evaluates false/unknown.
            and all(item.operator == "is_null" for item in lookup_where_filters)
        ),
        lookup_filters_in_on=bool(join_on_filter_items),
        lookup_filters_in_where=bool(lookup_where_filters),
    )
    compiled = CompiledPostgresQuery(
        sql="\n".join(sql_parts),
        parameters=tuple(binder.values),
        source_id=validated.source_id,
        snapshot_id=validated.snapshot_id,
        asset_id=validated.asset_id,
        expected_output_columns=tuple(outputs),
        effective_limit=plan.limit,
        ordering=ordering,
        semantics=semantics,
        fingerprint=_compile_fingerprint(validated),
        join=(
            CompiledPostgresJoin(
                relationship_id=validated.join.relationship_id,
                relationship_version_id=validated.join.relationship_version_id,
                version_number=validated.join.version_number,
                join_type=validated.join.join_type,
                lookup_asset_id=validated.join.lookup_asset_id,
                lookup_schema_name=validated.join.lookup_schema_name,
                lookup_table_name=validated.join.lookup_table_name,
                column_pairs=tuple(
                    (pair.base_column.id, pair.lookup_column.id)
                    for pair in validated.join.column_pairs
                ),
                foreign_key_constraint_name=validated.join.foreign_key_constraint_name,
                foreign_key_base_column_names=tuple(
                    pair.base_column.name for pair in validated.join.column_pairs
                ),
                foreign_key_lookup_column_names=tuple(
                    pair.lookup_column.name for pair in validated.join.column_pairs
                ),
                target_key_constraint_name=validated.join.target_key_constraint_name,
                target_key_kind=validated.join.target_key_kind,
                target_key_column_names=validated.join.target_key_column_names,
            )
            if validated.join is not None
            else None
        ),
    )
    object.__setattr__(compiled, "_seal", _COMPILED_QUERY_SEAL)
    return compiled


def _is_compiler_minted_postgres_query(value: object) -> bool:
    """Reject accidental hand-built compiled artifacts at the transport boundary."""

    return isinstance(value, CompiledPostgresQuery) and value._seal is _COMPILED_QUERY_SEAL


async def validate_and_compile_postgres_query(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    source_id: UUID,
    snapshot_id: UUID,
    plan: PostgresQueryPlan,
    settings: Settings,
) -> CompiledPostgresQuery:
    """The single Stage 4A application entry point; still no execution path."""

    validated = await validate_postgres_query_plan(
        session,
        owner_user_id=owner_user_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        plan=plan,
        settings=settings,
    )
    return _compile_validated_postgres_query(validated)


__all__ = [
    "CompiledPostgresJoin",
    "CompiledPostgresOrdering",
    "CompiledPostgresQuery",
    "ExpectedPostgresOutputColumn",
    "PostgresCalculationSemantics",
    "PostgresQueryPlanValidationError",
    "validate_and_compile_postgres_query",
    "validate_postgres_query_plan",
]
