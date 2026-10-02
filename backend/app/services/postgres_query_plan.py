"""Typed, non-executable planning contract for one PostgreSQL catalog asset.

This is intentionally separate from ``agentic_engine.conversational.QueryPlan``.
That contract is for uploaded files and is compiled to Polars; this one is
resolved against a durable PostgreSQL catalog before a later stage may execute
anything.  It deliberately contains no user identity, source credentials,
source ID, or snapshot ID.  Those are trusted application context supplied to
the validator, never planner-controlled input.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import math
from typing import Annotated, Literal, TypeAlias
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)


class IntegerValue(BaseModel):
    """A JSON integer deliberately distinct from decimal and float values."""

    model_config = _MODEL_CONFIG

    kind: Literal["integer"]
    value: StrictInt


class DecimalValue(BaseModel):
    """An exact decimal, encoded as a JSON string to preserve precision."""

    model_config = _MODEL_CONFIG

    kind: Literal["decimal"]
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def _require_decimal_string_or_decimal(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
            raise ValueError("decimal values must be encoded as a decimal string")
        if isinstance(value, str) and len(value) > 1024:
            raise ValueError("decimal values are too large")
        return value

    @field_validator("value")
    @classmethod
    def _require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("decimal values must be finite")
        if len(value.as_tuple().digits) > 1000 or abs(value.as_tuple().exponent) > 1000:
            raise ValueError("decimal values are too large")
        return value


class NumberValue(BaseModel):
    """A finite JSON number for PostgreSQL float columns."""

    model_config = _MODEL_CONFIG

    kind: Literal["number"]
    value: float

    @field_validator("value", mode="before")
    @classmethod
    def _require_json_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("number values must be JSON numbers")
        return value

    @field_validator("value")
    @classmethod
    def _require_finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("number values must be finite")
        return value


class BooleanValue(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["boolean"]
    value: StrictBool


class TextValue(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["text"]
    value: StrictStr = Field(min_length=0, max_length=4096)

    @field_validator("value")
    @classmethod
    def _require_postgres_safe_bounded_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("text values must not contain NUL bytes")
        try:
            utf8_value = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("text values must be valid UTF-8") from exc
        if len(utf8_value) > 4096:
            raise ValueError("text values must not exceed 4096 UTF-8 bytes")
        return value


class UUIDValue(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["uuid"]
    value: UUID

    @field_validator("value", mode="before")
    @classmethod
    def _require_uuid_string_or_uuid(cls, value: object) -> object:
        if not isinstance(value, (str, UUID)):
            raise ValueError("uuid values must be UUID strings")
        return value


class DateValue(BaseModel):
    model_config = _MODEL_CONFIG

    kind: Literal["date"]
    value: date

    @field_validator("value", mode="before")
    @classmethod
    def _parse_iso_date(cls, value: object) -> date:
        if isinstance(value, datetime):
            raise ValueError("date values must not include a time or timezone")
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            if "T" in value or " " in value:
                raise ValueError("date values must not include a time or timezone")
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date values must be ISO-8601 dates") from exc
        raise ValueError("date values must be ISO-8601 dates")


def _parse_iso_datetime(value: object, *, label: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{label} values must be ISO-8601 datetimes")
    if "T" not in value and " " not in value:
        # ``datetime.fromisoformat('2025-01-01')`` would otherwise silently
        # turn a date-only boundary into midnight. Date and timestamp plans
        # intentionally use distinct typed representations.
        raise ValueError(f"{label} values must include a time")
    # Python's parser accepts the RFC 3339 Z suffix beginning with 3.11.  The
    # replacement keeps this contract portable to application runtimes that do
    # not yet do so themselves.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} values must be ISO-8601 datetimes") from exc


class TimestampValue(BaseModel):
    """A timestamp without time zone; offsets are intentionally rejected."""

    model_config = _MODEL_CONFIG

    kind: Literal["timestamp"]
    value: datetime

    @field_validator("value", mode="before")
    @classmethod
    def _parse_timestamp(cls, value: object) -> datetime:
        return _parse_iso_datetime(value, label="timestamp")

    @field_validator("value")
    @classmethod
    def _require_naive_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("timestamp values must not include a timezone offset")
        return value


class TimestamptzValue(BaseModel):
    """An absolute timestamp normalized to UTC before it reaches asyncpg."""

    model_config = _MODEL_CONFIG

    kind: Literal["timestamptz"]
    value: datetime

    @field_validator("value", mode="before")
    @classmethod
    def _parse_timestamptz(cls, value: object) -> datetime:
        return _parse_iso_datetime(value, label="timestamptz")

    @field_validator("value")
    @classmethod
    def _require_aware_timestamptz(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamptz values must include a timezone offset")
        return value.astimezone(timezone.utc)


TypedPostgresValue: TypeAlias = Annotated[
    IntegerValue
    | DecimalValue
    | NumberValue
    | BooleanValue
    | TextValue
    | UUIDValue
    | DateValue
    | TimestampValue
    | TimestamptzValue,
    Field(discriminator="kind"),
]


FilterOperator = Literal[
    "eq",
    "neq",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "not_in",
    "contains",
    "not_contains",
    "is_null",
    "is_not_null",
]
AggregationOperation = Literal[
    "count_all",
    "count_column",
    "sum",
    "average",
    "minimum",
    "maximum",
]
TimeBucket = Literal["hour", "day", "week", "month", "quarter", "year"]
SortDirection = Literal["asc", "desc"]
SortOutputKind = Literal["projection", "grouping", "aggregation"]


class PostgresProjection(BaseModel):
    model_config = _MODEL_CONFIG

    column_id: UUID


class PostgresFilter(BaseModel):
    """One row-level predicate; there is intentionally no HAVING equivalent."""

    model_config = _MODEL_CONFIG

    column_id: UUID
    operator: FilterOperator
    value: TypedPostgresValue | None = None
    values: tuple[TypedPostgresValue, ...] | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _validate_value_shape(self) -> "PostgresFilter":
        null_operators = {"is_null", "is_not_null"}
        membership_operators = {"in", "not_in"}
        if self.operator in null_operators:
            if self.value is not None or self.values is not None:
                raise ValueError(f"{self.operator} does not accept values")
        elif self.operator in membership_operators:
            if self.value is not None or not self.values:
                raise ValueError(f"{self.operator} requires a non-empty values list")
        elif self.value is None or self.values is not None:
            raise ValueError(f"{self.operator} requires exactly one typed value")
        return self


class PostgresAggregation(BaseModel):
    model_config = _MODEL_CONFIG

    operation: AggregationOperation
    column_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_column_shape(self) -> "PostgresAggregation":
        if self.operation == "count_all":
            if self.column_id is not None:
                raise ValueError("count_all must not specify a column")
        elif self.column_id is None:
            raise ValueError(f"{self.operation} requires a column")
        return self


class PostgresGroupDimension(BaseModel):
    model_config = _MODEL_CONFIG

    column_id: UUID
    bucket: TimeBucket | None = None
    time_zone: StrictStr | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_bucket_shape(self) -> "PostgresGroupDimension":
        if self.bucket is None and self.time_zone is not None:
            raise ValueError("time_zone requires a time bucket")
        if self.time_zone is not None:
            try:
                ZoneInfo(self.time_zone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("time_zone must be a valid IANA timezone") from exc
        return self


class PostgresSort(BaseModel):
    """A compiler-owned output position, never a planner-provided SQL alias."""

    model_config = _MODEL_CONFIG

    output_kind: SortOutputKind
    output_index: StrictInt = Field(ge=0)
    direction: SortDirection


class PostgresQueryPlan(BaseModel):
    """The deliberately narrow, non-executable PostgreSQL query language.

    The enclosing application supplies source/snapshot/actor context to the
    validator.  The plan only identifies a catalog asset and its columns.
    """

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    asset_id: UUID
    projections: tuple[PostgresProjection, ...] = Field(default_factory=tuple, max_length=100)
    filters: tuple[PostgresFilter, ...] = Field(default_factory=tuple, max_length=50)
    aggregations: tuple[PostgresAggregation, ...] = Field(default_factory=tuple, max_length=25)
    group_by: tuple[PostgresGroupDimension, ...] = Field(default_factory=tuple, max_length=12)
    sort: PostgresSort | None = None
    limit: StrictInt = Field(ge=1, le=10000)

    @model_validator(mode="after")
    def _validate_query_shape(self) -> "PostgresQueryPlan":
        projection_ids = [projection.column_id for projection in self.projections]
        if len(projection_ids) != len(set(projection_ids)):
            raise ValueError("projections must not repeat a column")

        group_ids = [dimension.column_id for dimension in self.group_by]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("group_by must not repeat a column")

        aggregation_keys = [
            (aggregation.operation, aggregation.column_id)
            for aggregation in self.aggregations
        ]
        if len(aggregation_keys) != len(set(aggregation_keys)):
            raise ValueError("aggregations must not repeat an operation and column")

        if not self.projections and not self.aggregations:
            raise ValueError("a plan requires at least one projection or aggregation")
        if self.aggregations and self.projections:
            raise ValueError("projections cannot be combined with aggregations")
        if self.group_by and not self.aggregations:
            raise ValueError("group_by requires at least one aggregation")
        return self


__all__ = [
    "AggregationOperation",
    "BooleanValue",
    "DateValue",
    "DecimalValue",
    "FilterOperator",
    "IntegerValue",
    "NumberValue",
    "PostgresAggregation",
    "PostgresFilter",
    "PostgresGroupDimension",
    "PostgresProjection",
    "PostgresQueryPlan",
    "PostgresSort",
    "SortDirection",
    "SortOutputKind",
    "TextValue",
    "TimeBucket",
    "TimestampValue",
    "TimestamptzValue",
    "TypedPostgresValue",
    "UUIDValue",
]
