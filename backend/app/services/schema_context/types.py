"""Input and output types of the schema context builder (SPEC-06 B1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence
from uuid import UUID


class SchemaContextError(Exception):
    """``code`` is one of ``empty_scope``, ``over_budget``, ``catalog_unavailable``."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    ordinal: int
    native_type: str                 # CatalogColumn.formatted_native_type, as stored
    nullable: bool
    comment: str | None = None
    sample_values: tuple[str, ...] | None = None    # Part 4 slot


@dataclass(frozen=True)
class SchemaKey:
    kind: Literal["PRIMARY_KEY", "UNIQUE"]
    name: str                         # constraint name (sort key only; never rendered)
    columns: tuple[str, ...]          # in key order


@dataclass(frozen=True)
class SchemaForeignKey:
    name: str
    columns: tuple[str, ...]
    in_scope: bool
    target_schema: str | None = None
    target_table: str | None = None
    target_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchemaTable:
    schema: str
    name: str
    comment: str | None = None
    columns: tuple[SchemaColumn, ...] = ()
    keys: tuple[SchemaKey, ...] = ()
    foreign_keys: tuple[SchemaForeignKey, ...] = ()


@dataclass(frozen=True)
class MetricFilterInput:
    schema: str
    table: str
    column: str
    operator: str
    # (kind, python value); kind in integer/decimal/number/boolean/text/uuid/date/timestamp/timestamptz
    values: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class SchemaMetric:
    """A metric already resolved against the snapshot. ``render_schema`` decides whether it can be shown."""

    name: str
    normalized_name: str
    aggregation: str                  # count_all | count_column | sum | average | minimum | maximum
    schema: str
    table: str
    column: str | None
    filters: tuple[MetricFilterInput, ...] = ()
    unit: str | None = None
    date_column: str | None = None    # column of the same table
    time_zone: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class SchemaCatalog:
    tables: tuple[SchemaTable, ...]
    metrics: tuple[SchemaMetric, ...] = ()
    pre_skipped: tuple[tuple[str, str, str], ...] = ()    # skips decided while loading (e.g. a resolver error)


class SampleValueProvider(Protocol):
    """Part 4 plugs in here. Returns up to a few example values per column name, or nothing."""

    async def sample_values(self, schema: str, table: str, columns: Sequence[str]) -> dict[str, Sequence[str]]: ...


@dataclass(frozen=True)
class SchemaContext:
    text: str
    token_count: int
    fingerprint: str                  # sha256 of text
    source_id: UUID | None
    snapshot_id: UUID | None
    tables: tuple[tuple[str, str], ...]
    dropped: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str, str], ...] = ()
    per_table_tokens: tuple[tuple[tuple[str, str], int], ...] = field(default=())
