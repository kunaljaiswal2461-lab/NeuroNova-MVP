"""SPEC-09 L2-6 (run here on SQLite): the AI-settings and column-privacy APIs, and their effect on samples."""
from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import select

os.environ.setdefault("API_KEY", "privacy-route-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.source import DataSource
from app.db.models.user import User
from app.services import column_privacy
from app.services.sample_values.store import stored_sample_values
from app.services.catalog_service import get_current_catalog_snapshot_for_owner
from main import create_app
from tests.sample_values.conftest import FakeReader, factory, settings as _unused_settings, world  # noqa: F401


@pytest.fixture
async def api(world):
    await world.sync(FakeReader())
    settings = Settings(
        _env_file=None, api_key="privacy-route-test-key", database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="privacy-route-test-jwt-secret",
    )
    app = create_app()

    async def override_session():
        async with world.factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings

    async def headers(user_id):
        async with world.factory() as session:
            user = await session.get(User, user_id)
        return {"Authorization": "Bearer " + create_access_token(
            str(user.id), user.email, user.role.value, settings.jwt_signing_key, 30)}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, world, headers
    app.dependency_overrides.clear()


async def sample_values_of(world, table="orders", column="status"):
    async with world.factory() as session:
        snapshot = await get_current_catalog_snapshot_for_owner(session, source_id=world.source_id, owner_user_id=world.owner_id)
        provider = await stored_sample_values(session, source_id=world.source_id, snapshot_id=snapshot.id)
    return (await provider.sample_values("sales", table, [column])).get(column)


# ── ai-settings ───────────────────────────────────────────────────────────────

async def test_consent_is_off_by_default_and_the_owner_can_switch_it(api):
    client, world, headers = api
    url = f"/api/v1/sources/{world.source_id}/ai-settings"
    body = (await client.get(url, headers=await headers(world.owner_id))).json()
    assert body == {"result_rows_to_model": False, "sample_values_enabled": True, "result_rows_to_model_generation": 0}
    on = (await client.put(url, headers=await headers(world.owner_id), json={"result_rows_to_model": True})).json()
    assert on["result_rows_to_model"] is True and on["result_rows_to_model_generation"] == 1
    same = (await client.put(url, headers=await headers(world.owner_id), json={"result_rows_to_model": True})).json()
    assert same["result_rows_to_model_generation"] == 1                       # no change, no bump
    off = (await client.put(url, headers=await headers(world.owner_id), json={"result_rows_to_model": False})).json()
    assert off["result_rows_to_model"] is False and off["result_rows_to_model_generation"] == 2
    empty = (await client.put(url, headers=await headers(world.owner_id), json={})).json()
    assert empty["result_rows_to_model_generation"] == 2


async def test_another_user_gets_404_and_nothing_changes(api):
    client, world, headers = api
    url = f"/api/v1/sources/{world.source_id}/ai-settings"
    for method in ("get", "put"):
        kwargs = {"json": {"result_rows_to_model": True}} if method == "put" else {}
        response = await getattr(client, method)(url, headers=await headers(world.other_id), **kwargs)
        assert response.status_code == 404
    assert (await client.get(url, headers=await headers(world.owner_id))).json()["result_rows_to_model"] is False
    assert (await client.get(url)).status_code in (401, 403)


async def test_unknown_fields_and_wrong_types_are_refused(api):
    client, world, headers = api
    url = f"/api/v1/sources/{world.source_id}/ai-settings"
    assert (await client.put(url, headers=await headers(world.owner_id), json={"result_rows_to_model": "yes"})).status_code == 422
    assert (await client.put(url, headers=await headers(world.owner_id), json={"sample_values_enabled": False})).status_code == 422


async def test_read_consent_fails_closed(api):
    _client, world, _headers = api
    assert await column_privacy.read_consent(world.factory, source_id=world.source_id, owner_user_id=world.owner_id) == (False, 0)
    async with world.factory() as session:
        await column_privacy.update_ai_settings(session, source_id=world.source_id, owner_user_id=world.owner_id, result_rows_to_model=True)
    assert await column_privacy.read_consent(world.factory, source_id=world.source_id, owner_user_id=world.owner_id) == (True, 1)
    assert await column_privacy.read_consent(world.factory, source_id=world.source_id, owner_user_id=world.other_id) == (False, 0)

    def broken():
        raise RuntimeError("no database")

    assert await column_privacy.read_consent(broken, source_id=world.source_id, owner_user_id=world.owner_id) == (False, 0)


# ── column-privacy ────────────────────────────────────────────────────────────

async def test_the_list_has_text_columns_only_with_suggestions(api):
    client, world, headers = api
    body = (await client.get(f"/api/v1/sources/{world.source_id}/column-privacy", headers=await headers(world.owner_id))).json()
    by = {(r["schema"], r["table"], r["column"]): r for r in body}
    assert ("sales", "customers", "customer_id") not in by                    # integer: not listed
    assert by[("sales", "customers", "email")]["suggested_personal"] is True
    assert by[("sales", "customers", "full_name")]["suggested_personal"] is True
    assert by[("sales", "orders", "note")]["suggested_personal"] is True      # free text by name
    assert by[("sales", "customers", "segment")]["suggested_personal"] is False
    assert by[("sales", "orders", "status")] == {
        "schema": "sales", "table": "orders", "column": "status", "type": "varchar", "suggested_personal": False,
        "state": None, "effective_personal": False,
    }


async def test_an_override_wins_and_null_clears_it(api):
    client, world, headers = api
    url = f"/api/v1/sources/{world.source_id}/column-privacy"
    owner = await headers(world.owner_id)
    put = await client.put(url, headers=owner, json={"changes": [
        {"schema": "sales", "table": "orders", "column": "status", "state": "personal"},
        {"schema": "sales", "table": "orders", "column": "note", "state": "not_personal"},
    ]})
    assert put.status_code == 200
    by = {(r["table"], r["column"]): r for r in put.json()}
    assert by[("orders", "status")]["state"] == "personal" and by[("orders", "status")]["effective_personal"] is True
    assert by[("orders", "note")]["state"] == "not_personal" and by[("orders", "note")]["effective_personal"] is False
    assert by[("orders", "note")]["suggested_personal"] is True               # the suggestion is still shown
    cleared = await client.put(url, headers=owner, json={"changes": [
        {"schema": "sales", "table": "orders", "column": "status", "state": None}]})
    by = {(r["table"], r["column"]): r for r in cleared.json()}
    assert by[("orders", "status")]["state"] is None and by[("orders", "status")]["effective_personal"] is False


async def test_bad_changes_are_refused_and_change_nothing(api):
    client, world, headers = api
    url = f"/api/v1/sources/{world.source_id}/column-privacy"
    owner = await headers(world.owner_id)
    unknown = await client.put(url, headers=owner, json={"changes": [
        {"schema": "sales", "table": "orders", "column": "status", "state": "personal"},
        {"schema": "sales", "table": "orders", "column": "ghost", "state": "personal"}]})
    assert unknown.status_code == 422
    integer = await client.put(url, headers=owner, json={"changes": [
        {"schema": "sales", "table": "customers", "column": "customer_id", "state": "personal"}]})
    assert integer.status_code == 422
    assert (await client.put(url, headers=owner, json={"changes": [
        {"schema": "sales", "table": "orders", "column": "status", "state": "maybe"}]})).status_code == 422
    assert (await client.put(url, headers=await headers(world.other_id), json={"changes": []})).status_code == 404
    body = (await client.get(url, headers=owner)).json()
    assert all(r["state"] is None for r in body)


async def test_the_override_survives_a_resync(api):
    client, world, headers = api
    url = f"/api/v1/sources/{world.source_id}/column-privacy"
    owner = await headers(world.owner_id)
    await client.put(url, headers=owner, json={"changes": [{"schema": "sales", "table": "orders", "column": "status", "state": "personal"}]})
    await world.sync(FakeReader())
    body = (await client.get(url, headers=owner)).json()
    assert {(r["table"], r["column"]): r["state"] for r in body}[("orders", "status")] == "personal"


# ── samples follow the flag ───────────────────────────────────────────────────

async def test_a_personal_flag_hides_stored_samples_at_once_and_a_clear_brings_them_back(api):
    client, world, headers = api
    owner = await headers(world.owner_id)
    url = f"/api/v1/sources/{world.source_id}/column-privacy"
    assert await sample_values_of(world) is not None
    await client.put(url, headers=owner, json={"changes": [{"schema": "sales", "table": "orders", "column": "status", "state": "personal"}]})
    assert await sample_values_of(world) is None                               # no re-sync needed
    await client.put(url, headers=owner, json={"changes": [{"schema": "sales", "table": "orders", "column": "status", "state": None}]})
    assert await sample_values_of(world) is not None


async def test_the_free_text_suggestion_masks_in_pass_2_but_does_not_hide_samples(api, monkeypatch):
    client, world, headers = api
    async with world.factory() as session:
        flags = await column_privacy.load_effective_flags(session, world.source_id)
    assert flags.is_personal("sales", "orders", "note") is True                # free text by name: masked in pass 2
    assert flags.is_personal("sales", "orders", "status") is False
    assert flags.is_personal("sales", "orders", "order_id") is False           # not a text column, no name rule
    # Even if EVERY text column were suggested personal, stored samples are unaffected: they follow Part 4's rules,
    # owner exclusions and an explicit owner "personal" mark only.
    monkeypatch.setattr(column_privacy, "suggested_personal", lambda *a, **k: True)
    assert await sample_values_of(world, column="status") is not None
    owner = await headers(world.owner_id)
    await client.put(f"/api/v1/sources/{world.source_id}/column-privacy", headers=owner, json={"changes": [
        {"schema": "sales", "table": "orders", "column": "status", "state": "personal"}]})
    assert await sample_values_of(world, column="status") is None              # an explicit owner mark hides them
