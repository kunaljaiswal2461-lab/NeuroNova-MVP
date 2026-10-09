"""Loads the current catalog head into a ``SchemaCatalog`` and renders it (SPEC-06 B1, B7).

Authorization is ``load_catalog_scope``'s (owner, current head, unchanged scope); nothing is re-implemented.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogDeclaredKey,
    CatalogDeclaredKeyColumn,
    CatalogForeignKey,
    CatalogForeignKeyColumn,
    CatalogForeignKeyTargetStatus,
)
from app.db.models.source import DataSource
from app.services import metric_service
from app.services.sample_values.store import SampleValuesPersistenceError, stored_sample_values
from app.services.sql_guard.scope import load_catalog_scope

from .render import render_schema
from .tokenizer import O200K_COUNTER, TokenCounter
from .types import (
    MetricFilterInput,
    SampleValueProvider,
    SchemaCatalog,
    SchemaColumn,
    SchemaContext,
    SchemaContextError,
    SchemaForeignKey,
    SchemaKey,
    SchemaMetric,
    SchemaTable,
)

_TEXT_KINDS = {"integer", "decimal", "number", "boolean", "text", "uuid", "date", "timestamp", "timestamptz"}


def metric_from_resolved(resolved: Any, column_names: dict[uuid.UUID, str]) -> SchemaMetric:
    """Turn a ``ResolvedMetricDefinition`` into plain data. Raises ``KeyError`` for an unknown column id."""
    filters = []
    for item in resolved.fixed_filters:
        raw = [item.value] if item.value is not None else list(item.values or ())
        filters.append(
            MetricFilterInput(
                schema=resolved.asset_schema_name,
                table=resolved.asset_table_name,
                column=column_names[item.column_id],
                operator=item.operator,
                values=tuple((v.kind, v.value) for v in raw),
            )
        )
    return SchemaMetric(
        name=resolved.display_name,
        normalized_name=metric_service.normalize_metric_name(resolved.display_name),
        aggregation=resolved.aggregation,
        schema=resolved.asset_schema_name,
        table=resolved.asset_table_name,
        column=resolved.aggregation_column_name,
        filters=tuple(filters),
        unit=resolved.unit_label,
        date_column=resolved.default_date_column_name,
        time_zone=resolved.default_time_zone,
        description=resolved.description,
    )


async def _load_metrics(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    settings: Settings,
    column_names: dict[uuid.UUID, str],
) -> tuple[list[SchemaMetric], list[tuple[str, str, str]]]:
    skipped: list[tuple[str, str, str]] = []
    metrics: list[SchemaMetric] = []
    try:
        views = await metric_service.list_metric_definitions_for_owner(
            session, source_id=source_id, owner_user_id=owner_user_id, include_retired=False, settings=settings
        )
    except metric_service.MetricServiceError:
        return [], [("metric", "*", "metrics_unavailable")]
    for view in views:
        if view.state != "valid":           # NEEDS_REVIEW (and RETIRED) are never shown
            continue
        try:
            resolved = await metric_service.resolve_metric_definition_for_owner(
                session, source_id=source_id, metric_id=view.metric_id, owner_user_id=owner_user_id, settings=settings
            )
            if resolved.snapshot_id != snapshot_id:
                skipped.append(("metric", view.name, "stale_snapshot"))
                continue
            metrics.append(metric_from_resolved(resolved, column_names))
        except metric_service.MetricServiceError as exc:
            skipped.append(("metric", view.name, f"resolve_failed:{type(exc).__name__}"))
        except KeyError:
            skipped.append(("metric", view.name, "column_not_in_context"))
    return metrics, skipped


async def _attach_samples(
    tables: list[SchemaTable], provider: SampleValueProvider | None
) -> list[SchemaTable]:
    if provider is None:
        return tables
    out = []
    for table in tables:
        found = await provider.sample_values(table.schema, table.name, [c.name for c in table.columns])
        columns = tuple(
            SchemaColumn(
                c.name, c.ordinal, c.native_type, c.nullable, c.comment,
                tuple(found[c.name]) if found.get(c.name) else None,
            )
            for c in table.columns
        )
        out.append(SchemaTable(table.schema, table.name, table.comment, columns, table.keys, table.foreign_keys))
    return out


class _StoredDefault:
    """Marks "use the stored sample values when the source has them switched on" (SPEC-07 B6)."""

    def __repr__(self) -> str:
        return "STORED"


STORED_SAMPLES = _StoredDefault()


async def _resolve_samples(
    session: AsyncSession,
    sample_values: SampleValueProvider | None | _StoredDefault,
    *,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> SampleValueProvider | None:
    if sample_values is not STORED_SAMPLES:
        return sample_values          # an explicit provider, or ``None`` = no samples (Part 5 A/B)
    try:
        enabled = (await session.execute(
            select(DataSource.sample_values_enabled).where(DataSource.id == source_id)
        )).scalar_one_or_none()
        if not enabled:
            return None
        return await stored_sample_values(session, source_id=source_id, snapshot_id=snapshot_id)
    except (SQLAlchemyError, SampleValuesPersistenceError):
        return None                   # example values are optional; the rest of the context is still right


async def build_schema_context(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    settings: Settings,
    sample_values: SampleValueProvider | None | _StoredDefault = STORED_SAMPLES,
    counter: TokenCounter | None = None,
) -> SchemaContext:
    # Raises the compiler's own errors unless the caller owns the source and ``snapshot_id`` is the head.
    scope = await load_catalog_scope(
        session, owner_user_id=owner_user_id, source_id=source_id, snapshot_id=snapshot_id
    )
    if not scope.tables:
        raise SchemaContextError("empty_scope")
    asset_ids = [t.asset_id for t in scope.tables]
    same = (CatalogAsset.source_id == source_id, CatalogAsset.snapshot_id == snapshot_id)
    try:
        assets = (await session.execute(
            select(CatalogAsset).where(*same, CatalogAsset.id.in_(asset_ids))
        )).scalars().all()
        columns = (await session.execute(
            select(CatalogColumn).where(
                CatalogColumn.source_id == source_id, CatalogColumn.snapshot_id == snapshot_id,
                CatalogColumn.asset_id.in_(asset_ids),
            )
        )).scalars().all()
        keys = (await session.execute(
            select(CatalogDeclaredKey).where(
                CatalogDeclaredKey.source_id == source_id, CatalogDeclaredKey.snapshot_id == snapshot_id,
                CatalogDeclaredKey.asset_id.in_(asset_ids),
            )
        )).scalars().all()
        key_columns = (await session.execute(
            select(CatalogDeclaredKeyColumn).where(
                CatalogDeclaredKeyColumn.source_id == source_id, CatalogDeclaredKeyColumn.snapshot_id == snapshot_id,
                CatalogDeclaredKeyColumn.asset_id.in_(asset_ids),
            )
        )).scalars().all()
        fks = (await session.execute(
            select(CatalogForeignKey).where(
                CatalogForeignKey.source_id == source_id, CatalogForeignKey.snapshot_id == snapshot_id,
                CatalogForeignKey.referencing_asset_id.in_(asset_ids),
            )
        )).scalars().all()
        fk_columns = (await session.execute(
            select(CatalogForeignKeyColumn).where(
                CatalogForeignKeyColumn.source_id == source_id, CatalogForeignKeyColumn.snapshot_id == snapshot_id,
                CatalogForeignKeyColumn.referencing_asset_id.in_(asset_ids),
            )
        )).scalars().all()

        # Plain data now: later calls may roll the session back and expire these rows.
        asset_by_id = {a.id: (a.schema_name, a.table_name, a.table_comment) for a in assets}
        col_by_id = {c.id: (c.asset_id, c.column_name) for c in columns}
        cols_by_asset: dict[uuid.UUID, list[SchemaColumn]] = {}
        for c in columns:
            cols_by_asset.setdefault(c.asset_id, []).append(
                SchemaColumn(c.column_name, c.ordinal_position, c.formatted_native_type, c.is_nullable, c.column_comment)
            )
        key_cols: dict[uuid.UUID, list[tuple[int, str]]] = {}
        for kc in key_columns:
            key_cols.setdefault(kc.key_id, []).append((kc.ordinal_position, col_by_id[kc.column_id][1]))
        keys_by_asset: dict[uuid.UUID, list[SchemaKey]] = {}
        for k in keys:
            ordered = tuple(n for _, n in sorted(key_cols.get(k.id, ())))
            keys_by_asset.setdefault(k.asset_id, []).append(SchemaKey(k.key_kind.value, k.constraint_name, ordered))
        fk_cols: dict[uuid.UUID, list[tuple[int, str, str]]] = {}
        for fc in fk_columns:
            fk_cols.setdefault(fc.foreign_key_id, []).append(
                (fc.ordinal_position, col_by_id[fc.referencing_column_id][1], col_by_id[fc.referenced_column_id][1])
            )
        fks_by_asset: dict[uuid.UUID, list[SchemaForeignKey]] = {}
        for fk in fks:
            ordered = sorted(fk_cols.get(fk.id, ()))
            in_scope = fk.target_status is CatalogForeignKeyTargetStatus.IN_SCOPE and fk.referenced_asset_id in asset_by_id
            target = asset_by_id.get(fk.referenced_asset_id) if in_scope else None
            fks_by_asset.setdefault(fk.referencing_asset_id, []).append(
                SchemaForeignKey(
                    fk.constraint_name,
                    tuple(r for _, r, _ in ordered),
                    in_scope,
                    target[0] if target else None,
                    target[1] if target else None,
                    tuple(t for _, _, t in ordered) if in_scope else (),
                )
            )
    except SQLAlchemyError:
        raise SchemaContextError("catalog_unavailable") from None
    except KeyError:
        raise SchemaContextError("catalog_unavailable") from None

    tables = [
        SchemaTable(
            asset_by_id[a][0], asset_by_id[a][1], asset_by_id[a][2],
            tuple(cols_by_asset.get(a, ())), tuple(keys_by_asset.get(a, ())), tuple(fks_by_asset.get(a, ())),
        )
        for a in asset_ids
        if a in asset_by_id
    ]
    tables = await _attach_samples(
        tables, await _resolve_samples(session, sample_values, source_id=source_id, snapshot_id=snapshot_id)
    )

    column_names = {cid: name for cid, (_, name) in col_by_id.items()}
    metrics, metric_skips = await _load_metrics(
        session, owner_user_id=owner_user_id, source_id=source_id, snapshot_id=snapshot_id,
        settings=settings, column_names=column_names,
    )
    return render_schema(
        SchemaCatalog(tuple(tables), tuple(metrics), tuple(metric_skips)),
        budget_tokens=settings.nl_sql_schema_budget_tokens,
        counter=counter or O200K_COUNTER,
        source_id=source_id,
        snapshot_id=snapshot_id,
    )
