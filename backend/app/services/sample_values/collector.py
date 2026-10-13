"""Collect sample values for one sync (SPEC-07 B2). Best-effort: it never raises for a source problem."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Sequence
from typing import Any, Protocol

from app.connectors.postgres_adapter import DiscoveredTable
from app.connectors.postgres_sample_adapter import (
    SampleColumnSpec,
    SampleTableRequest,
    SampleTableResult,
)
from app.core.config import Settings

from .pii import column_name_is_pii
from .select import (
    TEXT_NATIVE_TYPES,
    Sample,
    decide_from_exact,
    decide_from_pg_stats,
)
from .types import ColumnKey, SampleCollection


class SampleReader(Protocol):
    async def read_table(self, **kwargs: Any) -> SampleTableResult: ...


def candidate_columns(
    table: DiscoveredTable, excluded: Collection[ColumnKey]
) -> tuple[list[Any], dict[ColumnKey, str]]:
    """Split a table's columns into those worth reading and the ones skipped before any query (B2 step 1)."""
    keep: list[Any] = []
    skips: dict[ColumnKey, str] = {}
    for column in table.columns:
        if column.native_type_name not in TEXT_NATIVE_TYPES or column.native_type_schema not in (None, "pg_catalog"):
            continue                                    # not a text-family column: not a candidate, not reported
        key = (table.schema_name, table.table_name, column.column_name)
        if key in excluded:
            skips[key] = "excluded_by_owner"
        elif column_name_is_pii(table.table_name, column.column_name):
            skips[key] = "pii_name"
        else:
            keep.append(column)
    return keep, skips


def decide_table(result: SampleTableResult, names: Sequence[str], settings: Settings) -> dict[str, Sample]:
    """Turn one table's raw reads into a decision per column."""
    out: dict[str, Sample] = {}
    for name in names:
        if name in result.stats:
            stat = result.stats[name]
            out[name] = decide_from_pg_stats(
                n_distinct=stat.n_distinct,
                null_frac=stat.null_frac,
                vals=stat.vals,
                freqs=stat.freqs,
                reltuples=result.reltuples,
                max_distinct=settings.sample_values_max_distinct,
                min_coverage=settings.sample_values_min_coverage,
            )
        elif name in result.exact:
            out[name] = decide_from_exact(
                groups=result.exact[name], max_distinct=settings.sample_values_max_distinct
            )
        else:
            out[name] = Sample(skip="no_statistics")
    return out


async def collect_sample_values(
    *,
    reader: SampleReader,
    connection: dict[str, Any],
    tables: Sequence[DiscoveredTable],
    excluded: Collection[ColumnKey],
    generation: int,
    settings: Settings,
) -> SampleCollection:
    """``connection`` holds host/port/database_name/username/password/custom_ca_pem (the discovery credentials)."""
    collection = SampleCollection(generation=generation)
    deadline = time.monotonic() + settings.sample_values_total_timeout_seconds
    for table in tables:
        keep, early = candidate_columns(table, excluded)
        collection.skips.update(early)
        if not keep:
            continue
        keys = {c.column_name: (table.schema_name, table.table_name, c.column_name) for c in keep}
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise TimeoutError
            result = await asyncio.wait_for(
                reader.read_table(
                    **connection,
                    request=SampleTableRequest(
                        table.schema_name,
                        table.table_name,
                        tuple(
                            SampleColumnSpec(
                                c.column_name, c.native_type_schema, c.native_type_name,
                                c.native_type_oid, c.native_type_modifier, c.is_nullable,
                            )
                            for c in keep
                        ),
                    ),
                    max_distinct=settings.sample_values_max_distinct,
                    exact_max_bytes=settings.sample_values_exact_max_bytes,
                ),
                timeout=remaining,
            )
            decided = decide_table(result, list(keys), settings)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001 - best effort by contract (I3)
            # A permission denial, a timeout, a schema change, a bad statistics row: the table is skipped and
            # the sync goes on. Nothing from the exception (driver text can carry values) is kept.
            for key in keys.values():
                collection.skips[key] = "error"
            continue
        for name, sample in decided.items():
            if sample.skip is None:
                collection.samples[keys[name]] = sample
            else:
                collection.skips[keys[name]] = sample.skip
    return collection
