"""Catalog scope builder for the SQL validator (SPEC-04 §9).

Authorization is not re-implemented here. Owner, current-head and unchanged-scope checks are the
ones ``validate_postgres_query_plan`` already relies on; this module only reads the in-scope
tables and columns after those checks pass.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.catalog import CatalogColumn
from app.services import catalog_service

from .types import CatalogScope, ScopeColumn, ScopeTable


async def load_catalog_scope(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> CatalogScope:
    """Return every selected table of the source's *current* snapshot, with exact column names.

    Raises what the query compiler raises: ``NotFoundError`` for a source the caller does not own or a
    snapshot that does not belong to the source, ``CatalogQueryContextError`` for a historical snapshot,
    an unpublished run, or a changed table selection.
    """
    context = await catalog_service.get_postgres_chat_catalog_context_for_owner(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    if context.snapshot.id != snapshot_id:
        # Ask the compiler's own resolver which error this snapshot deserves, rather than guessing.
        await catalog_service.resolve_postgres_query_catalog_context_for_owner(
            session,
            source_id=source_id,
            snapshot_id=snapshot_id,
            asset_id=context.assets[0].id,
            column_ids=(),
            owner_user_id=owner_user_id,
        )
        raise catalog_service.CatalogQueryContextError()

    asset_ids = [asset.id for asset in context.assets]
    try:
        rows = (
            await session.execute(
                select(CatalogColumn)
                .where(
                    CatalogColumn.source_id == source_id,
                    CatalogColumn.snapshot_id == snapshot_id,
                    CatalogColumn.asset_id.in_(asset_ids),
                )
                .order_by(CatalogColumn.asset_id, CatalogColumn.ordinal_position, CatalogColumn.id)
            )
        ).scalars().all()
    except SQLAlchemyError:
        raise catalog_service.CatalogPersistenceError() from None

    columns_by_asset: dict[uuid.UUID, list[ScopeColumn]] = {}
    for column in rows:
        columns_by_asset.setdefault(column.asset_id, []).append(
            ScopeColumn(
                column.column_name, column.native_type_name or column.formatted_native_type, column.id
            )
        )
    return CatalogScope(
        source_id=source_id,
        snapshot_id=snapshot_id,
        tables=tuple(
            ScopeTable(
                asset.schema_name, asset.table_name, tuple(columns_by_asset.get(asset.id, ())), asset.id
            )
            for asset in context.assets
        ),
    )
