"""SPEC-03 §16.6 B1 — bootstrap creates the source and syncs the catalog."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models.catalog import CatalogAsset, CatalogColumn, CatalogForeignKey, CatalogSnapshot
from app.db.models.source import DataSource, SourceCredential, SourceStatus
from evals.bootstrap import IN_SCOPE_TABLES

pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio(loop_scope="session")]


async def test_b1_bootstrap_creates_source_and_synced_catalog(fixture_context):
    ctx = fixture_context
    async with ctx.session_factory() as session:
        source = await session.get(DataSource, ctx.source_id)
        assert source.owner_user_id == ctx.user_id
        assert source.host == "client-postgres.nn-eval.test" and source.has_custom_ca is True
        assert source.status == SourceStatus.TESTED

        snapshot = await session.get(CatalogSnapshot, ctx.snapshot_id)
        assert snapshot.source_id == ctx.source_id

        assets = (
            await session.execute(select(CatalogAsset).where(CatalogAsset.snapshot_id == ctx.snapshot_id))
        ).scalars().all()
        names = {(a.schema_name, a.table_name) for a in assets}
        assert names == {("shop", t) for t in IN_SCOPE_TABLES}          # and nothing out of scope

        by_table = {}
        columns = (
            await session.execute(
                select(CatalogColumn).where(CatalogColumn.asset_id.in_([a.id for a in assets]))
            )
        ).scalars().all()
        asset_name = {a.id: a.table_name for a in assets}
        for c in columns:
            by_table.setdefault(asset_name[c.asset_id], set()).add(c.column_name)
        assert by_table["orders"] >= {"id", "customer_id", "status", "ordered_at", "delivered_on", "total_amount"}
        assert by_table["Order Events"] == {"id", "order", "Event Type", "occurred_at"}

        fks = (
            await session.execute(select(CatalogForeignKey).where(CatalogForeignKey.snapshot_id == ctx.snapshot_id))
        ).scalars().all()
        assert len(fks) == 6

        credential = (
            await session.execute(select(SourceCredential).where(SourceCredential.source_id == ctx.source_id))
        ).scalar_one()
        assert b"nn_reader" not in credential.ciphertext
