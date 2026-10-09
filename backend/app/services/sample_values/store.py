"""Persistence and owner controls for sample values (SPEC-07 B1, B4, B6).

Values live in ``catalog_column_samples``, one row per (snapshot, column). They go to the model through
``stored_sample_values``; no function in this module returns a value to the API layer.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogColumnSample,
    CatalogSnapshot,
    SourceCatalogHead,
)
from app.db.models.source import DataSource, SourceSampleExclusion
from app.exceptions.custom_exceptions import AppError, NotFoundError

from .pii import column_name_is_pii
from .types import ColumnKey, SampleCollection

MAX_EXCLUSIONS = 2000


class SampleValuesError(AppError):
    pass


class SampleValuesInputError(SampleValuesError):
    status_code = 422
    code = "invalid_sample_values_request"

    def __init__(self, message: str = "sample values request is invalid"):
        super().__init__(message)


class SampleValuesPersistenceError(SampleValuesError):
    status_code = 503
    code = "sample_values_persistence_failed"

    def __init__(self):
        super().__init__("could not complete the sample values request")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def skips_payload(collection: SampleCollection) -> list[list[str]]:
    """The cache stored on the snapshot: names and a fixed reason, sorted. No value."""
    return [[s, t, c, r] for (s, t, c), r in sorted(collection.skips.items())]


def add_sample_rows(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    column_ids: dict[ColumnKey, uuid.UUID],
    collection: SampleCollection,
) -> int:
    """Add rows for the kept columns of ``collection`` to the publishing transaction. Returns the row count."""
    collected_at = _now()
    added = 0
    for key, sample in sorted(collection.samples.items()):
        column_id = column_ids.get(key)
        if column_id is None or not sample.values:
            continue
        session.add(
            CatalogColumnSample(
                snapshot_id=snapshot_id,
                column_id=column_id,
                source_id=source_id,
                values=[{"v": v, "f": f} for v, f in sample.values],
                method=sample.method,
                coverage=round(float(sample.coverage), 4),
                collected_at=collected_at,
                sample_values_generation=collection.generation,
            )
        )
        added += 1
    return added


# ── reading for the schema context (B6) ─────────────────────────────────────────────────────────────

class StoredSampleValues:
    """``SampleValueProvider`` backed by ``catalog_column_samples`` for one snapshot (plain data, read once)."""

    def __init__(self, by_table: dict[tuple[str, str], dict[str, tuple[str, ...]]]):
        self._by_table = by_table

    async def sample_values(self, schema: str, table: str, columns: Sequence[str]) -> dict[str, Sequence[str]]:
        found = self._by_table.get((schema, table), {})
        return {c: found[c] for c in columns if c in found}


async def load_exclusions(session: AsyncSession, source_id: uuid.UUID) -> set[ColumnKey]:
    rows = (
        await session.execute(
            select(
                SourceSampleExclusion.schema_name, SourceSampleExclusion.table_name, SourceSampleExclusion.column_name
            ).where(SourceSampleExclusion.source_id == source_id)
        )
    ).all()
    return {(r[0], r[1], r[2]) for r in rows}


async def stored_sample_values(
    session: AsyncSession, *, source_id: uuid.UUID, snapshot_id: uuid.UUID
) -> StoredSampleValues:
    """Values of one snapshot as a provider. Exclusions and name rules apply at read time, so an exclusion added
    after collection hides the column immediately (I1). Raises ``SampleValuesPersistenceError`` on a database error."""
    try:
        rows = (
            await session.execute(
                select(
                    CatalogAsset.schema_name, CatalogAsset.table_name, CatalogColumn.column_name,
                    CatalogColumnSample.values,
                )
                .join(CatalogColumn, (CatalogColumn.source_id == CatalogColumnSample.source_id)
                      & (CatalogColumn.snapshot_id == CatalogColumnSample.snapshot_id)
                      & (CatalogColumn.id == CatalogColumnSample.column_id))
                .join(CatalogAsset, (CatalogAsset.source_id == CatalogColumn.source_id)
                      & (CatalogAsset.snapshot_id == CatalogColumn.snapshot_id)
                      & (CatalogAsset.id == CatalogColumn.asset_id))
                .where(CatalogColumnSample.source_id == source_id, CatalogColumnSample.snapshot_id == snapshot_id)
            )
        ).all()
        excluded = await load_exclusions(session, source_id)
    except SQLAlchemyError:
        await _rollback(session)
        raise SampleValuesPersistenceError() from None
    by_table: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {}
    for schema, table, column, values in rows:
        if (schema, table, column) in excluded or column_name_is_pii(table, column):
            continue
        items = [item["v"] for item in values if isinstance(item, dict) and isinstance(item.get("v"), str)]
        if items:
            by_table.setdefault((schema, table), {})[column] = tuple(items)
    return StoredSampleValues(by_table)


# ── owner controls (B4) ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SampleValuesState:
    enabled: bool
    generation: int
    excluded_columns: tuple[ColumnKey, ...]
    # (schema, table, column, method, value_count) for the head snapshot; counts only.
    collected: tuple[tuple[str, str, str, str, int], ...]
    skipped: tuple[tuple[str, str, str, str], ...]


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:                                   # noqa: BLE001
        pass


async def _owned_source(session: AsyncSession, source_id: uuid.UUID, owner_id: uuid.UUID, *, lock: bool) -> DataSource:
    statement = select(DataSource).where(DataSource.id == source_id)
    if isinstance(owner_id, uuid.UUID):
        statement = statement.where(DataSource.owner_user_id == owner_id)
    else:
        statement = statement.where(DataSource.id.is_(None))
    if lock:
        statement = statement.with_for_update()
    source = (await session.execute(statement)).scalar_one_or_none()
    if source is None:
        await _rollback(session)
        raise NotFoundError("source not found")
    return source


async def get_sample_values_state(
    session: AsyncSession, *, source_id: uuid.UUID, owner_user_id: uuid.UUID
) -> SampleValuesState:
    """Counts and names for the head snapshot. Never a value."""
    try:
        source = await _owned_source(session, source_id, owner_user_id, lock=False)
        enabled, generation = bool(source.sample_values_enabled), int(source.sample_values_generation)
        excluded = await load_exclusions(session, source_id)
        head = await session.get(SourceCatalogHead, source_id)
        collected: list[tuple[str, str, str, str, int]] = []
        skips: dict[ColumnKey, str] = {}
        if head is not None:
            rows = (
                await session.execute(
                    select(
                        CatalogAsset.schema_name, CatalogAsset.table_name, CatalogColumn.column_name,
                        CatalogColumnSample.method, CatalogColumnSample.values,
                    )
                    .join(CatalogColumn, (CatalogColumn.source_id == CatalogColumnSample.source_id)
                          & (CatalogColumn.snapshot_id == CatalogColumnSample.snapshot_id)
                          & (CatalogColumn.id == CatalogColumnSample.column_id))
                    .join(CatalogAsset, (CatalogAsset.source_id == CatalogColumn.source_id)
                          & (CatalogAsset.snapshot_id == CatalogColumn.snapshot_id)
                          & (CatalogAsset.id == CatalogColumn.asset_id))
                    .where(CatalogColumnSample.source_id == source_id,
                           CatalogColumnSample.snapshot_id == head.snapshot_id)
                )
            ).all()
            for schema, table, column, method, values in sorted(rows, key=lambda r: tuple(r[:3])):
                if (schema, table, column) in excluded or column_name_is_pii(table, column):
                    continue
                collected.append((schema, table, column, method, len(values)))
            snapshot = await session.get(CatalogSnapshot, head.snapshot_id)
            for entry in (snapshot.sample_skips or []) if snapshot is not None else []:
                if isinstance(entry, list) and len(entry) == 4:
                    skips[(entry[0], entry[1], entry[2])] = entry[3]
        for key in excluded:                            # an exclusion added after collection shows immediately
            skips[key] = "excluded_by_owner"
        await session.rollback()                        # read-only: release the connection
    except SQLAlchemyError:
        await _rollback(session)
        raise SampleValuesPersistenceError() from None
    return SampleValuesState(
        enabled=enabled,
        generation=generation,
        excluded_columns=tuple(sorted(excluded)),
        collected=tuple(collected),
        skipped=tuple((s, t, c, r) for (s, t, c), r in sorted(skips.items())),
    )


def _validate_exclusions(columns: Sequence[ColumnKey]) -> set[ColumnKey]:
    if len(columns) > MAX_EXCLUSIONS:
        raise SampleValuesInputError("too many excluded columns")
    out: set[ColumnKey] = set()
    for item in columns:
        if len(item) != 3 or any(not isinstance(p, str) or not p or len(p) > 63 or "\x00" in p for p in item):
            raise SampleValuesInputError("an excluded column needs a schema, table and column name")
        out.add((item[0], item[1], item[2]))
    return out


async def _delete_samples_for_columns(session: AsyncSession, source_id: uuid.UUID, keys: set[ColumnKey]) -> None:
    """Delete stored samples of the named columns in EVERY snapshot of the source."""
    for schema, table, column in sorted(keys):
        ids = (
            await session.execute(
                select(CatalogColumn.id)
                .join(CatalogAsset, (CatalogAsset.source_id == CatalogColumn.source_id)
                      & (CatalogAsset.snapshot_id == CatalogColumn.snapshot_id)
                      & (CatalogAsset.id == CatalogColumn.asset_id))
                .where(CatalogColumn.source_id == source_id, CatalogAsset.schema_name == schema,
                       CatalogAsset.table_name == table, CatalogColumn.column_name == column)
            )
        ).scalars().all()
        if ids:
            await session.execute(
                delete(CatalogColumnSample).where(
                    CatalogColumnSample.source_id == source_id, CatalogColumnSample.column_id.in_(ids)
                )
            )


async def configure_sample_values_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    enabled: bool | None = None,
    excluded_columns: Sequence[ColumnKey] | None = None,
) -> DataSource:
    """Change the switch and/or REPLACE the exclusion list. Never contacts the source.

    Any actual change bumps ``sample_values_generation``. Switching off deletes every stored sample of the
    source in the same transaction; a new exclusion deletes that column's samples at once.
    """
    if enabled is not None and not isinstance(enabled, bool):
        raise SampleValuesInputError()
    wanted = _validate_exclusions(excluded_columns) if excluded_columns is not None else None
    try:
        source = await _owned_source(session, source_id, owner_user_id, lock=True)
        changed = False
        if enabled is not None and source.sample_values_enabled != enabled:
            source.sample_values_enabled = enabled
            changed = True
        if enabled is False:
            await session.execute(delete(CatalogColumnSample).where(CatalogColumnSample.source_id == source.id))
        if wanted is not None:
            current = await load_exclusions(session, source.id)
            added, removed = wanted - current, current - wanted
            for schema, table, column in sorted(removed):
                await session.execute(
                    delete(SourceSampleExclusion).where(
                        SourceSampleExclusion.source_id == source.id,
                        SourceSampleExclusion.schema_name == schema,
                        SourceSampleExclusion.table_name == table,
                        SourceSampleExclusion.column_name == column,
                    )
                )
            for schema, table, column in sorted(added):
                session.add(
                    SourceSampleExclusion(
                        source_id=source.id, schema_name=schema, table_name=table, column_name=column,
                        created_by_user_id=owner_user_id,
                    )
                )
            if added:
                await session.flush()
                await _delete_samples_for_columns(session, source.id, added)
            changed = changed or bool(added or removed)
        if changed:
            source.sample_values_generation += 1
        await session.commit()
        return source
    except SQLAlchemyError:
        await _rollback(session)
        raise SampleValuesPersistenceError() from None


async def count_sample_rows(session: AsyncSession, source_id: uuid.UUID) -> int:
    return int(
        (await session.execute(
            select(func.count()).select_from(CatalogColumnSample).where(CatalogColumnSample.source_id == source_id)
        )).scalar_one()
    )
