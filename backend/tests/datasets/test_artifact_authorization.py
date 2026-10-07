"""HTTP authorization regression tests for dataset artifacts and visualization."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from jose import jwt

# ``main`` constructs an app at import time, so provide the required settings
# before importing the application factory.
os.environ.setdefault("API_KEY", "artifact-auth-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.api.routes import dataset_routes
from app.core.config import Settings, get_settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models.dataset import DatasetRecord, DatasetStatus, FileType
from app.db.models.user import User, UserRole
from agentic_engine.vector_store.models import RetrievalResult
from main import create_app


@dataclass
class _Dataset:
    id: uuid.UUID
    user_id: uuid.UUID | None
    original_name: str
    status: DatasetStatus = DatasetStatus.COMPLETE
    filename: str = "private.csv"
    file_type: FileType = FileType.CSV
    size_bytes: int = 42
    progress_pct: int = 100
    row_count: int | None = 2
    col_count: int | None = 2
    error_message: str | None = None
    uploaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    profiled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class _Result:
    def __init__(self, records: list[_Dataset], *, scalar: int | None = None):
        self.records = records
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self.records

    def scalar(self):
        return self._scalar


class _AuthorizationSession:
    """Minimal async DB session for real auth + dataset service execution."""

    def __init__(self, users: list[User], datasets: list[_Dataset]):
        self.users = {user.id: user for user in users}
        self.datasets = {dataset.id: dataset for dataset in datasets}
        self.executed = []

    async def get(self, model, identity):
        if model is User:
            return self.users.get(identity)
        if model is DatasetRecord:
            return self.datasets.get(identity)
        return None

    async def execute(self, statement):
        self.executed.append(statement)
        records = self._records_for_scope(statement)
        if "count(" in str(statement).lower():
            return _Result([], scalar=len(records))
        return _Result(records)

    def _records_for_scope(self, statement) -> list[_Dataset]:
        rendered = str(statement).upper()
        records = list(self.datasets.values())
        if "USER_ID IS NULL" in rendered:
            return [record for record in records if record.user_id is None]

        params = statement.compile().params.values()
        owner_ids = {value for value in params if isinstance(value, uuid.UUID)}
        if owner_ids:
            return [record for record in records if record.user_id in owner_ids]
        return records


@dataclass
class _ArtifactCalls:
    profile: int = 0
    findings: int = 0
    viz: int = 0
    insights: int = 0
    pdf: int = 0
    retrieve: int = 0

    def assert_not_called(self) -> None:
        assert self == _ArtifactCalls()


@dataclass
class _Context:
    app: object
    settings: Settings
    session: _AuthorizationSession
    owner: User
    other: User
    owned: _Dataset
    other_dataset: _Dataset
    legacy: _Dataset
    calls: _ArtifactCalls

    def headers_for(self, user: User) -> dict[str, str]:
        token = create_access_token(
            str(user.id),
            user.email,
            user.role.value,
            self.settings.jwt_signing_key,
            self.settings.access_token_expire_minutes,
        )
        return {"Authorization": f"Bearer {token}"}

    @property
    def legacy_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.settings.api_key}


def _profile(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": "2026-10-03T00:00:00+00:00",
        "schema": {"row_count": 2, "col_count": 2, "columns": []},
        "health": {"score": 95, "grade": "A", "components": {}},
        "secret": "owner-only profile",
    }


def _findings(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": "2026-10-03T00:00:00+00:00",
        "findings": [{"title": "owner-only finding"}],
    }


def _viz(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": "2026-10-03T00:00:00+00:00",
        "charts": [],
        "skipped_columns": [],
        "total_columns": 2,
        "rendered_columns": 0,
        "secret": "owner-only viz",
    }


def _insights(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": "2026-10-03T00:00:00+00:00",
        "executive_summary": {"headline": "owner-only insight"},
        "finding_explanations": [],
        "suggested_questions": [],
    }


@pytest.fixture
def artifact_context(monkeypatch, tmp_path) -> _Context:
    owner = User(
        id=uuid.uuid4(),
        email="owner@example.test",
        name="Owner",
        is_active=True,
        is_verified=True,
        role=UserRole.USER,
    )
    other = User(
        id=uuid.uuid4(),
        email="other@example.test",
        name="Other",
        is_active=True,
        is_verified=True,
        role=UserRole.USER,
    )
    owned = _Dataset(
        id=uuid.uuid4(), user_id=owner.id, original_name="owner-private.csv"
    )
    other_dataset = _Dataset(
        id=uuid.uuid4(), user_id=other.id, original_name="other-private.csv"
    )
    legacy = _Dataset(
        id=uuid.uuid4(), user_id=None, original_name="legacy-unowned.csv"
    )
    settings = Settings(
        api_key="artifact-auth-test-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        jwt_secret_key="artifact-auth-test-secret",
        data_dir=tmp_path,
    )
    settings.viz_dir.mkdir(parents=True, exist_ok=True)
    (settings.viz_dir / f"{owned.id}.json").write_text(
        json.dumps(_viz(owned.id)), encoding="utf-8"
    )
    (settings.viz_dir / f"{legacy.id}.json").write_text(
        json.dumps(_viz(legacy.id)), encoding="utf-8"
    )
    # A flat raster file used to be exposed by the global chart lister. It is
    # intentionally not attributable to a dataset and must stay unreachable.
    (settings.viz_dir / "another-users-chart.png").write_bytes(b"not-an-image")

    calls = _ArtifactCalls()

    def fake_load_profile(dataset_id, _settings):
        calls.profile += 1
        return _profile(dataset_id)

    def fake_load_findings(dataset_id, _settings):
        calls.findings += 1
        return _findings(dataset_id)

    def fake_load_viz(dataset_id, _settings):
        calls.viz += 1
        return _viz(dataset_id)

    def fake_load_insights(dataset_id, _settings):
        calls.insights += 1
        return _insights(dataset_id)

    def fake_build_pdf(**_kwargs):
        calls.pdf += 1
        return b"owner-only PDF"

    async def fake_retrieve(dataset_id, query, **_kwargs):
        calls.retrieve += 1
        return RetrievalResult(
            dataset_id=dataset_id,
            query=query.query,
            hits=[],
            model_used="test-retriever",
            degraded=True,
        )

    monkeypatch.setattr(dataset_routes, "load_report", fake_load_profile)
    monkeypatch.setattr(dataset_routes, "load_findings_raw", fake_load_findings)
    monkeypatch.setattr(dataset_routes, "load_viz_raw", fake_load_viz)
    monkeypatch.setattr(dataset_routes, "load_insights_raw", fake_load_insights)
    monkeypatch.setattr(dataset_routes, "build_summary_pdf", fake_build_pdf)
    monkeypatch.setattr(dataset_routes, "run_retrieve", fake_retrieve)

    session = _AuthorizationSession([owner, other], [owned, other_dataset, legacy])
    application = create_app()

    async def override_session():
        yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_settings] = lambda: settings

    yield _Context(
        application,
        settings,
        session,
        owner,
        other,
        owned,
        other_dataset,
        legacy,
        calls,
    )
    application.dependency_overrides.clear()


@pytest.fixture
async def client(artifact_context):
    transport = httpx.ASGITransport(app=artifact_context.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _request_path(dataset_id: uuid.UUID, kind: str) -> str:
    return {
        "status": f"/api/v1/datasets/{dataset_id}/status",
        "metadata": f"/api/v1/datasets/{dataset_id}",
        "profile": f"/api/v1/datasets/{dataset_id}/profile",
        "findings": f"/api/v1/datasets/{dataset_id}/findings",
        "viz": f"/api/v1/datasets/{dataset_id}/viz",
        "insights": f"/api/v1/datasets/{dataset_id}/insights",
        "pdf": f"/api/v1/datasets/{dataset_id}/summary.pdf",
        "retrieve": f"/api/v1/datasets/{dataset_id}/retrieve",
        "legacy_viz": f"/viz/{dataset_id}.json",
    }[kind]


async def _request(client, dataset_id: uuid.UUID, kind: str, headers: dict | None):
    method = "POST" if kind == "retrieve" else "GET"
    return await client.request(
        method,
        _request_path(dataset_id, kind),
        headers=headers,
        json={"query": "private finding"} if kind == "retrieve" else None,
    )


def _signed_access_token(settings: Settings, **claims) -> str:
    """Build a correctly signed token with intentionally malformed claims."""
    return jwt.encode(
        {
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            **claims,
        },
        settings.jwt_signing_key,
        algorithm="HS256",
    )


@pytest.mark.parametrize(
    "kind",
    [
        "status",
        "metadata",
        "profile",
        "findings",
        "viz",
        "insights",
        "pdf",
        "retrieve",
        "legacy_viz",
    ],
)
async def test_owner_can_read_its_dataset_and_artifacts(client, artifact_context, kind):
    response = await _request(
        client,
        artifact_context.owned.id,
        kind,
        artifact_context.headers_for(artifact_context.owner),
    )

    assert response.status_code == 200
    if kind == "profile":
        assert response.json()["secret"] == "owner-only profile"
    elif kind == "findings":
        assert response.json()["findings"][0]["title"] == "owner-only finding"
    elif kind == "viz":
        assert response.json()["dataset_id"] == str(artifact_context.owned.id)
    elif kind == "legacy_viz":
        assert response.json()["secret"] == "owner-only viz"
        assert response.headers["cache-control"] == "no-store"
    elif kind == "insights":
        assert response.json()["executive_summary"]["headline"] == "owner-only insight"
    elif kind == "pdf":
        assert response.content == b"owner-only PDF"
    elif kind == "retrieve":
        assert response.json()["query"] == "private finding"


@pytest.mark.parametrize(
    "kind",
    [
        "status",
        "metadata",
        "profile",
        "findings",
        "viz",
        "insights",
        "pdf",
        "retrieve",
        "legacy_viz",
    ],
)
@pytest.mark.parametrize("actor", ["other", "legacy"])
async def test_other_user_and_global_key_cannot_read_owned_artifacts(
    client, artifact_context, kind, actor
):
    headers = (
        artifact_context.headers_for(artifact_context.other)
        if actor == "other"
        else artifact_context.legacy_headers
    )

    response = await _request(client, artifact_context.owned.id, kind, headers)

    assert response.status_code == 404
    assert "owner-only" not in response.text
    artifact_context.calls.assert_not_called()


@pytest.mark.parametrize(
    "kind",
    [
        "status",
        "metadata",
        "profile",
        "findings",
        "viz",
        "insights",
        "pdf",
        "retrieve",
        "legacy_viz",
    ],
)
async def test_unauthenticated_requests_cannot_read_dataset_artifacts(
    client, artifact_context, kind
):
    response = await _request(client, artifact_context.owned.id, kind, None)

    assert response.status_code == 401
    artifact_context.calls.assert_not_called()


@pytest.mark.parametrize(
    "authorization",
    ["Bearer", "Basic ignored", "Bearer-not-a-token", "Bearer invalid-token"],
)
async def test_malformed_authorization_cannot_downgrade_to_legacy_artifact_access(
    client, artifact_context, authorization
):
    response = await _request(
        client,
        artifact_context.legacy.id,
        "profile",
        {
            "Authorization": authorization,
            "X-API-Key": artifact_context.settings.api_key,
        },
    )

    assert response.status_code == 401
    artifact_context.calls.assert_not_called()


@pytest.mark.parametrize("claims", [{}, {"sub": "not-a-uuid"}, {"sub": None}])
async def test_malformed_signed_access_claims_return_401_not_500(
    client, artifact_context, claims
):
    token = _signed_access_token(artifact_context.settings, **claims)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-API-Key": artifact_context.settings.api_key,
    }

    dataset_response = await _request(
        client, artifact_context.owned.id, "profile", headers
    )
    account_response = await client.get("/api/v1/auth/me", headers=headers)

    assert dataset_response.status_code == 401
    assert account_response.status_code == 401
    artifact_context.calls.assert_not_called()


async def test_jwt_precedence_over_a_global_key_preserves_dataset_scope(
    client, artifact_context
):
    owner_headers = {
        **artifact_context.headers_for(artifact_context.owner),
        "X-API-Key": artifact_context.settings.api_key,
    }
    other_headers = {
        **artifact_context.headers_for(artifact_context.other),
        "X-API-Key": artifact_context.settings.api_key,
    }

    owner = await _request(
        client, artifact_context.owned.id, "profile", owner_headers
    )
    assert owner.status_code == 200

    artifact_context.calls.profile = 0
    other = await _request(
        client, artifact_context.owned.id, "profile", other_headers
    )
    assert other.status_code == 404
    artifact_context.calls.assert_not_called()


async def test_unknown_and_inaccessible_dataset_ids_are_non_disclosing(
    client, artifact_context
):
    other_headers = artifact_context.headers_for(artifact_context.other)
    inaccessible = await _request(
        client, artifact_context.owned.id, "profile", other_headers
    )
    unknown = await _request(client, uuid.uuid4(), "profile", other_headers)

    assert inaccessible.status_code == unknown.status_code == 404
    assert inaccessible.json()["error"]["code"] == "not_found"
    assert unknown.json()["error"]["code"] == "not_found"
    assert "owner-only" not in inaccessible.text
    assert "owner-only" not in unknown.text
    artifact_context.calls.assert_not_called()


async def test_invalid_legacy_key_cannot_read_dataset_artifacts(client, artifact_context):
    response = await _request(
        client,
        artifact_context.owned.id,
        "profile",
        {"X-API-Key": "not-the-configured-key"},
    )

    assert response.status_code == 401
    artifact_context.calls.assert_not_called()


async def test_upload_passes_only_trusted_auth_identity_to_first_persistence_step(
    client, artifact_context, monkeypatch
):
    captured: list[dict] = []
    profiled: list[uuid.UUID] = []

    async def fake_create_dataset_from_upload(**kwargs):
        captured.append(kwargs)
        return (
            artifact_context.owned
            if kwargs["owner_user_id"] is not None
            else artifact_context.legacy
        )

    async def fake_run_profiling(dataset_id):
        profiled.append(dataset_id)

    monkeypatch.setattr(
        dataset_routes.dataset_service,
        "create_dataset_from_upload",
        fake_create_dataset_from_upload,
    )
    monkeypatch.setattr(dataset_routes, "run_profiling", fake_run_profiling)

    owner = await client.post(
        "/api/v1/upload",
        headers=artifact_context.headers_for(artifact_context.owner),
        files={"file": ("private.csv", b"value\n1\n", "text/csv")},
    )
    legacy = await client.post(
        "/api/v1/upload",
        headers=artifact_context.legacy_headers,
        files={"file": ("legacy.csv", b"value\n1\n", "text/csv")},
    )

    assert owner.status_code == legacy.status_code == 202
    assert [call["owner_user_id"] for call in captured] == [
        artifact_context.owner.id,
        None,
    ]
    assert profiled == [artifact_context.owned.id, artifact_context.legacy.id]


async def test_dataset_lists_are_scoped_for_jwt_and_legacy_callers(
    client, artifact_context
):
    owner = await client.get(
        "/api/v1/datasets", headers=artifact_context.headers_for(artifact_context.owner)
    )
    other = await client.get(
        "/api/v1/datasets", headers=artifact_context.headers_for(artifact_context.other)
    )
    legacy = await client.get("/api/v1/datasets", headers=artifact_context.legacy_headers)
    unauthenticated = await client.get("/api/v1/datasets")

    assert owner.status_code == 200
    assert [item["dataset_id"] for item in owner.json()["items"]] == [
        str(artifact_context.owned.id)
    ]
    assert owner.json()["count"] == 1
    assert other.status_code == 200
    assert [item["dataset_id"] for item in other.json()["items"]] == [
        str(artifact_context.other_dataset.id)
    ]
    assert other.json()["count"] == 1
    assert legacy.status_code == 200
    assert [item["dataset_id"] for item in legacy.json()["items"]] == [
        str(artifact_context.legacy.id)
    ]
    assert legacy.json()["count"] == 1
    assert unauthenticated.status_code == 401


async def test_global_key_can_read_genuinely_unowned_legacy_artifacts(
    client, artifact_context
):
    profile = await _request(
        client, artifact_context.legacy.id, "profile", artifact_context.legacy_headers
    )
    raw_viz = await _request(
        client, artifact_context.legacy.id, "legacy_viz", artifact_context.legacy_headers
    )

    assert profile.status_code == 200
    assert profile.json()["secret"] == "owner-only profile"
    assert raw_viz.status_code == 200
    assert raw_viz.json()["secret"] == "owner-only viz"


async def test_jwt_user_cannot_read_an_unowned_legacy_artifact(
    client, artifact_context
):
    response = await _request(
        client,
        artifact_context.legacy.id,
        "profile",
        artifact_context.headers_for(artifact_context.owner),
    )

    assert response.status_code == 404
    artifact_context.calls.assert_not_called()


async def test_global_chart_lister_and_unscoped_static_assets_are_retired(
    client, artifact_context
):
    owner_headers = artifact_context.headers_for(artifact_context.owner)

    charts = await client.get(
        f"/api/v1/datasets/{artifact_context.owned.id}/charts", headers=owner_headers
    )
    raster = await client.get("/viz/another-users-chart.png", headers=owner_headers)
    anonymous_charts = await client.get(
        f"/api/v1/datasets/{artifact_context.owned.id}/charts"
    )
    anonymous_raster = await client.get("/viz/another-users-chart.png")

    assert charts.status_code == 404
    assert raster.status_code == 404
    assert anonymous_charts.status_code == 404
    assert anonymous_raster.status_code == 404
    assert "another-users-chart" not in charts.text
    assert "another-users-chart" not in raster.text
