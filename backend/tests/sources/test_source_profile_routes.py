"""JWT-only HTTP boundary tests for optional bounded source profiling."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("API_KEY", "stage6a-route-test-api-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.source_profile import (
    SourceProfileFindingSeverity,
    SourceProfileFindingType,
    SourceProfileObservationStatus,
    SourceProfilePopulation,
    SourceProfileRunStatus,
    SourceProfileSamplingMethod,
)
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.services import source_profile_service, source_service
from main import create_app


class _AuthSession:
    def __init__(self, users: dict[uuid.UUID, User]) -> None:
        self._users = users

    async def get(self, model, identity):
        return self._users.get(identity) if model is User else None


@pytest.fixture
def profile_route_context(monkeypatch):
    owner = User(
        id=uuid.uuid4(),
        email="profile-owner@example.test",
        name="Profile owner",
        role=UserRole.USER,
        is_active=True,
    )
    other = User(
        id=uuid.uuid4(),
        email="profile-other@example.test",
        name="Profile other",
        role=UserRole.USER,
        is_active=True,
    )
    settings = Settings(
        _env_file=None,
        api_key="stage6a-route-test-api-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="stage6a-route-test-jwt-secret",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id="test",
        connector_credentials_active_key="M2Secr7H2lWLMX6wAHxxJUT_lag81Dmkto5bDCoK1GU=",
    )
    now = datetime.now(timezone.utc)
    source_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    amount_id = uuid.uuid4()
    table_observation_id = uuid.uuid4()
    column_observation_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        profiling_enabled=False,
        profiling_generation=0,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        snapshot_id=snapshot_id,
        status=SourceProfileRunStatus.QUEUED,
        requested_at=now,
        started_at=None,
        completed_at=None,
        attempt_count=0,
        next_attempt_at=None,
        failure_code=None,
        sampling_method=SourceProfileSamplingMethod.SYSTEM_TABLE_SAMPLE,
        sample_percent=Decimal("1.0000"),
        sample_limit=500,
        sample_seed=7,
        max_operations=4,
        selected_asset_count=1,
        selected_column_count=1,
    )
    calls: list[tuple[str, uuid.UUID]] = []

    def _assert_owner(source_arg: uuid.UUID, owner_user_id: uuid.UUID) -> None:
        if source_arg != source_id or owner_user_id != owner.id:
            raise NotFoundError("source not found")

    async def fake_configure(_session, *, source_id: uuid.UUID, owner_user_id: uuid.UUID, profiling_enabled: bool):
        _assert_owner(source_id, owner_user_id)
        calls.append(("configure", owner_user_id))
        if source.profiling_enabled != profiling_enabled:
            source.profiling_enabled = profiling_enabled
            source.profiling_generation += 1
        return source

    async def fake_request(_session, *, source_id: uuid.UUID, owner_user_id: uuid.UUID, selections, settings):
        _assert_owner(source_id, owner_user_id)
        calls.append(("request", owner_user_id))
        assert settings is not None
        assert len(selections) == 1
        assert selections[0].asset_id == asset_id
        assert selections[0].column_ids == (amount_id,)
        return run

    async def fake_list_runs(_session, *, source_id: uuid.UUID, owner_user_id: uuid.UUID, page: int, page_size: int):
        _assert_owner(source_id, owner_user_id)
        calls.append(("list_runs", owner_user_id))
        return SimpleNamespace(source_id=source_id, page=page, page_size=page_size, total=1, runs=(run,))

    async def fake_get_run(_session, *, source_id: uuid.UUID, run_id: uuid.UUID, owner_user_id: uuid.UUID):
        _assert_owner(source_id, owner_user_id)
        if run_id != run.id:
            raise NotFoundError("profile run not found")
        calls.append(("get_run", owner_user_id))
        return run

    async def fake_cancel(_session, *, source_id: uuid.UUID, run_id: uuid.UUID, owner_user_id: uuid.UUID):
        checked = await fake_get_run(
            _session, source_id=source_id, run_id=run_id, owner_user_id=owner_user_id
        )
        checked.status = SourceProfileRunStatus.CANCELLED
        checked.completed_at = now
        calls.append(("cancel", owner_user_id))
        return checked

    table_observation = SimpleNamespace(
        id=table_observation_id,
        collected_at=now,
        sampling_method=SourceProfileSamplingMethod.SYSTEM_TABLE_SAMPLE,
        sample_percent=Decimal("1.0000"),
        sample_limit=500,
        sample_seed=7,
        population=SourceProfilePopulation.OBSERVED_SAMPLE,
        observed_row_count=5,
        status=SourceProfileObservationStatus.COMPLETE,
        warning_codes=["system_sampling_block_bias"],
    )
    column_observation = SimpleNamespace(
        id=column_observation_id,
        collected_at=now,
        population=SourceProfilePopulation.OBSERVED_SAMPLE,
        observed_row_count=5,
        null_count=1,
        non_null_count=4,
        null_rate=Decimal("0.2"),
        distinct_count=3,
        numeric_count=4,
        numeric_sum=Decimal("20"),
        numeric_minimum=Decimal("1"),
        numeric_maximum=Decimal("9"),
        numeric_mean=Decimal("5"),
        status=SourceProfileObservationStatus.COMPLETE,
        warning_codes=[],
    )
    asset = SimpleNamespace(id=asset_id, schema_name="sales", table_name="orders")
    column = SimpleNamespace(id=amount_id, column_name="amount", formatted_native_type="numeric")

    async def fake_observations(_session, *, source_id: uuid.UUID, run_id: uuid.UUID, owner_user_id: uuid.UUID, page: int, page_size: int):
        checked = await fake_get_run(
            _session, source_id=source_id, run_id=run_id, owner_user_id=owner_user_id
        )
        calls.append(("observations", owner_user_id))
        return SimpleNamespace(
            source_id=source_id,
            run=checked,
            page=page,
            page_size=page_size,
            total_tables=1,
            table_observations=(SimpleNamespace(observation=table_observation, asset=asset),),
            column_observations=(
                SimpleNamespace(
                    observation=column_observation,
                    table_observation=table_observation,
                    asset=asset,
                    column=column,
                    scalar_kind="numeric",
                ),
            ),
        )

    finding = SimpleNamespace(
        id=uuid.uuid4(),
        finding_type=SourceProfileFindingType.OBSERVED_NULL_RATE,
        severity=SourceProfileFindingSeverity.MEDIUM,
        title="Observed null values",
        description="20.00% of observed rows had a null value.",
        limitations="Exact for the bounded observed sample only.",
        evidence_json={"population": "OBSERVED_SAMPLE", "null_count": 1},
        generated_at=now,
    )

    async def fake_findings(_session, *, source_id: uuid.UUID, run_id: uuid.UUID, owner_user_id: uuid.UUID, page: int, page_size: int):
        checked = await fake_get_run(
            _session, source_id=source_id, run_id=run_id, owner_user_id=owner_user_id
        )
        calls.append(("findings", owner_user_id))
        return SimpleNamespace(
            source_id=source_id,
            run=checked,
            page=page,
            page_size=page_size,
            total=1,
            findings=(
                SimpleNamespace(
                    finding=finding,
                    table_observation=table_observation,
                    column_observation=column_observation,
                    asset=asset,
                    column=column,
                ),
            ),
        )

    credential_calls: list[Settings] = []
    monkeypatch.setattr(source_profile_service, "configure_source_profiling_for_owner", fake_configure)
    monkeypatch.setattr(source_profile_service, "request_source_profile_run_for_owner", fake_request)
    monkeypatch.setattr(source_profile_service, "list_source_profile_runs_for_owner", fake_list_runs)
    monkeypatch.setattr(source_profile_service, "get_source_profile_run_for_owner", fake_get_run)
    monkeypatch.setattr(source_profile_service, "cancel_source_profile_run_for_owner", fake_cancel)
    monkeypatch.setattr(source_profile_service, "list_source_profile_observations_for_owner", fake_observations)
    monkeypatch.setattr(source_profile_service, "list_source_profile_findings_for_owner", fake_findings)
    monkeypatch.setattr(
        source_service,
        "require_credential_operations",
        lambda value: credential_calls.append(value),
    )

    app = create_app()

    async def override_session():
        yield _AuthSession({owner.id: owner, other.id: other})

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings

    def headers(user: User) -> dict[str, str]:
        return {
            "Authorization": "Bearer "
            + create_access_token(
                str(user.id), user.email, user.role.value, settings.jwt_signing_key, 30
            )
        }

    yield SimpleNamespace(
        app=app,
        owner=owner,
        other=other,
        source_id=source_id,
        run=run,
        asset_id=asset_id,
        amount_id=amount_id,
        calls=calls,
        credential_calls=credential_calls,
        headers=headers,
    )
    app.dependency_overrides.clear()


@pytest.fixture
async def profile_route_client(profile_route_context):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=profile_route_context.app), base_url="http://test"
    ) as client:
        yield client


async def test_profile_routes_are_jwt_owner_only_and_never_accept_sql(
    profile_route_client, profile_route_context
):
    context = profile_route_context
    source_path = f"/api/v1/sources/{context.source_id}"
    owner_headers = context.headers(context.owner)

    configured = await profile_route_client.put(
        f"{source_path}/profiling",
        headers=owner_headers,
        json={"profiling_enabled": True},
    )
    assert configured.status_code == 200
    assert configured.json() == {
        "source_id": str(context.source_id),
        "profiling_enabled": True,
        "profiling_generation": 1,
        "historical_profiles_retained": True,
    }

    created = await profile_route_client.post(
        f"{source_path}/profile-runs",
        headers=owner_headers,
        json={"tables": [{"asset_id": str(context.asset_id), "column_ids": [str(context.amount_id)]}]},
    )
    assert created.status_code == 202
    assert created.json()["run_id"] == str(context.run.id)
    assert created.json()["population"] == "OBSERVED_SAMPLE"
    assert len(context.credential_calls) == 1

    invalid = await profile_route_client.post(
        f"{source_path}/profile-runs",
        headers=owner_headers,
        json={
            "tables": [{"asset_id": str(context.asset_id), "column_ids": [str(context.amount_id)]}],
            "sql": "SELECT * FROM sales.orders",
        },
    )
    assert invalid.status_code == 422
    assert "SELECT *" not in invalid.text
    assert [name for name, _actor in context.calls] == ["configure", "request"]

    api_key = await profile_route_client.get(
        f"{source_path}/profile-runs",
        headers={"X-API-Key": "stage6a-route-test-api-key"},
    )
    assert api_key.status_code == 401
    other = await profile_route_client.put(
        f"{source_path}/profiling",
        headers=context.headers(context.other),
        json={"profiling_enabled": True},
    )
    assert other.status_code == 404


async def test_profile_read_routes_render_only_safe_aggregate_contracts(
    profile_route_client, profile_route_context
):
    context = profile_route_context
    source_path = f"/api/v1/sources/{context.source_id}"
    headers = context.headers(context.owner)

    runs = await profile_route_client.get(f"{source_path}/profile-runs", headers=headers)
    assert runs.status_code == 200
    assert runs.json()["items"][0]["sample_seed"] == 7

    observations = await profile_route_client.get(
        f"{source_path}/profile-runs/{context.run.id}/observations", headers=headers
    )
    assert observations.status_code == 200
    observation_body = observations.json()
    assert observation_body["table_observations"][0]["population"] == "OBSERVED_SAMPLE"
    assert observation_body["column_observations"][0]["numeric_sum"] == "20"
    assert "raw_row" not in observations.text
    assert "sql_text" not in observations.text

    findings = await profile_route_client.get(
        f"{source_path}/profile-runs/{context.run.id}/findings", headers=headers
    )
    assert findings.status_code == 200
    finding = findings.json()["items"][0]
    assert finding["description"] == "20.00% of observed rows had a null value."
    assert finding["evidence"]["population"] == "OBSERVED_SAMPLE"
