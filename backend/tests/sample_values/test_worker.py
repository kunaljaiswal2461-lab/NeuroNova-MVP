"""SPEC-07 L1: collection through the real sync worker, the publish fence, and the invariants I1-I3, I5, I6."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db.models.catalog import (
    CatalogAsset,
    CatalogColumn,
    CatalogColumnSample,
    CatalogSnapshot,
    CatalogSyncRun,
    CatalogSyncRunStatus,
)
from app.db.models.source import DataSource
from app.services.catalog_service import get_current_catalog_snapshot_for_owner
from app.services.sample_values.store import configure_sample_values_for_owner, stored_sample_values

from .conftest import FakeReader


async def _stored(world, snapshot_id=None):
    async with world.factory() as session:
        head = await get_current_catalog_snapshot_for_owner(
            session, source_id=world.source_id, owner_user_id=world.owner_id)
        provider = await stored_sample_values(session, source_id=world.source_id, snapshot_id=head.id)
    return provider._by_table, head.id


async def test_sync_stores_decisions_and_keeps_values_byte_exact(world):
    reader = FakeReader()
    await world.sync(reader)
    by_table, snapshot_id = await _stored(world)
    assert by_table[("sales", "customers")] == {"segment": ("Retail", "SMB", "Enterprise", "enterprise ")}   # I6
    assert by_table[("sales", "orders")] == {"status": ("PAID", "Cancelled", "pending", "REFUNDED")}
    async with world.factory() as session:
        snap = await session.get(CatalogSnapshot, snapshot_id)
        skips = {tuple(e[:3]): e[3] for e in snap.sample_skips}
        row = (await session.execute(select(CatalogColumnSample).where(
            CatalogColumnSample.snapshot_id == snapshot_id))).scalars().first()
    assert skips == {
        ("sales", "customers", "full_name"): "pii_name",
        ("sales", "customers", "email"): "pii_name",
        ("sales", "customers", "city"): "high_cardinality",
        ("sales", "orders", "note"): "pii_values",
    }
    assert row.method == "pg_stats" and float(row.coverage) > 0.99
    # I1: the PII-named and PII-valued columns were not even asked for / not stored.
    assert ("sales", "customers", ("segment", "city")) in reader.calls
    assert "someone@example.com" not in repr(by_table)


async def test_i2_switch_off_stores_nothing_and_leaves_zero_rows(world):
    await world.sync(FakeReader())
    async with world.factory() as session:
        assert (await session.execute(select(func.count()).select_from(CatalogColumnSample))).scalar_one() == 2
        await configure_sample_values_for_owner(
            session, source_id=world.source_id, owner_user_id=world.owner_id, enabled=False)
        assert (await session.execute(select(func.count()).select_from(CatalogColumnSample))).scalar_one() == 0
    reader = FakeReader()
    await world.sync(reader)
    assert reader.calls == []                                      # the source was not even read
    async with world.factory() as session:
        assert (await session.execute(select(func.count()).select_from(CatalogColumnSample))).scalar_one() == 0
        snap_skips = (await session.execute(select(CatalogSnapshot.sample_skips))).scalars().all()
    assert snap_skips[-1] is None


async def test_exclusion_is_applied_before_reading_and_survives_resyncs(world):
    async with world.factory() as session:
        await configure_sample_values_for_owner(
            session, source_id=world.source_id, owner_user_id=world.owner_id,
            excluded_columns=[("sales", "orders", "status")])
    reader = FakeReader()
    await world.sync(reader)
    await world.sync(reader)
    assert all("status" not in cols for _, _, cols in reader.calls)
    by_table, _ = await _stored(world)
    assert "status" not in by_table.get(("sales", "orders"), {})


async def test_i3_a_failing_table_is_skipped_and_the_snapshot_still_publishes(world):
    await world.sync(FakeReader(fail={("sales", "customers")}))
    async with world.factory() as session:
        run = (await session.execute(select(CatalogSyncRun))).scalars().one()
        assert run.status is CatalogSyncRunStatus.SUCCEEDED
        snap = (await session.execute(select(CatalogSnapshot))).scalars().one()
    skips = {tuple(e[:3]): e[3] for e in snap.sample_skips}
    assert skips[("sales", "customers", "segment")] == "error"
    assert skips[("sales", "customers", "city")] == "error"
    by_table, _ = await _stored(world)
    assert ("sales", "customers") not in by_table and by_table[("sales", "orders")]["status"][0] == "PAID"


async def test_i3_a_reader_that_blows_up_unexpectedly_does_not_fail_the_sync(world):
    class Boom(FakeReader):
        async def read_table(self, **kw):
            raise RuntimeError("driver said: password for user x is wrong; value 'secret'")

    await world.sync(Boom())
    async with world.factory() as session:
        run = (await session.execute(select(CatalogSyncRun))).scalars().one()
        snap = (await session.execute(select(CatalogSnapshot))).scalars().one()
    assert run.status is CatalogSyncRunStatus.SUCCEEDED
    assert "secret" not in repr(snap.sample_skips)
    assert {e[3] for e in snap.sample_skips} <= {"error", "pii_name"}


async def test_total_timeout_skips_the_rest_without_failing(world, settings):
    reader = FakeReader()

    async def slow(_key):
        await asyncio.sleep(5)

    reader.hook = slow
    world.settings.__dict__["sample_values_total_timeout_seconds"] = 0.05
    await world.sync(reader)
    async with world.factory() as session:
        run = (await session.execute(select(CatalogSyncRun))).scalars().one()
    assert run.status is CatalogSyncRunStatus.SUCCEEDED


async def test_i5_a_change_during_the_read_publishes_the_snapshot_without_samples(world):
    reader = FakeReader()

    async def owner_flips_the_switch(_key):
        async with world.factory() as session:
            await configure_sample_values_for_owner(
                session, source_id=world.source_id, owner_user_id=world.owner_id,
                excluded_columns=[("sales", "customers", "segment")])

    reader.hook = owner_flips_the_switch
    await world.sync(reader)
    async with world.factory() as session:
        assert (await session.execute(select(func.count()).select_from(CatalogSnapshot))).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(CatalogColumnSample))).scalar_one() == 0
        snap = (await session.execute(select(CatalogSnapshot))).scalars().one()
        source = await session.get(DataSource, world.source_id)
    assert snap.sample_skips is None and source.sample_values_generation >= 1


async def test_i5_the_fence_also_covers_a_switch_off_during_the_read(world):
    reader = FakeReader()

    async def off(_key):
        async with world.factory() as session:
            await configure_sample_values_for_owner(
                session, source_id=world.source_id, owner_user_id=world.owner_id, enabled=False)

    reader.hook = off
    await world.sync(reader)
    async with world.factory() as session:
        assert (await session.execute(select(func.count()).select_from(CatalogColumnSample))).scalar_one() == 0


async def test_new_snapshot_gets_new_rows_and_old_rows_stay_with_the_old_snapshot(world):
    await world.sync(FakeReader())
    await world.sync(FakeReader())
    async with world.factory() as session:
        assert (await session.execute(select(func.count()).select_from(CatalogColumnSample))).scalar_one() == 4
        assert (await session.execute(select(func.count(func.distinct(CatalogColumnSample.snapshot_id))))).scalar_one() == 2


async def test_cascade_from_catalog_columns(world):
    await world.sync(FakeReader())
    async with world.factory() as session:
        asset = (await session.execute(select(CatalogAsset).where(CatalogAsset.table_name == "orders"))).scalars().one()
        for column in (await session.execute(select(CatalogColumn).where(CatalogColumn.asset_id == asset.id))).scalars():
            await session.delete(column)
        await session.commit()
    async with world.factory() as session:
        remaining = (await session.execute(select(CatalogColumnSample))).scalars().all()
    assert len(remaining) == 1                       # customers.segment stays; orders.status went with its column
