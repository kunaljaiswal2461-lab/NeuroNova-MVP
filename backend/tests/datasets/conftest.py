"""Fixtures for HTTP-level dataset route tests.

Builds the real FastAPI app and overrides the DB session / settings / auth
dependencies so no live database or network is required. Artifact JSON
(profile / findings / insights) is written into a tmp ``data_dir`` exactly
where the persistence loaders expect it.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core.dependencies import get_auth_context
from app.db.models.dataset import DatasetStatus, FileType
from app.db.session import get_session
from main import create_app


# ── stub DB objects ──────────────────────────────────────────────────────────

@dataclass
class StubDatasetRecord:
    """Duck-typed stand-in for ``DatasetRecord`` (only route-used fields)."""

    id: uuid.UUID
    original_name: str = "sales_data.csv"
    file_type: FileType = FileType.CSV
    row_count: int | None = 20
    col_count: int | None = 10
    status: DatasetStatus = DatasetStatus.COMPLETE


class FakeSession:
    """Fake async session — ``get`` returns the configured record."""

    def __init__(self, record: StubDatasetRecord | None = None):
        self.record = record

    async def get(self, model, pk):
        if self.record is not None and self.record.id == pk:
            return self.record
        return None

    async def commit(self):  # pragma: no cover — not exercised here
        pass


# ── settings + artifact helpers ──────────────────────────────────────────────

@pytest.fixture
def dataset_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    settings = Settings(
        api_key="test-api-key-12345",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        openai_api_key="",
        data_dir=tmp_path,
    )
    for d in (settings.profiles_dir, settings.findings_dir, settings.llm_cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    return settings


def _write_json(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, default=str), encoding="utf-8")


def make_profile(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": {
            "row_count": 20,
            "col_count": 10,
            "columns": [
                {"name": "order_id", "dtype": "integer", "polars_dtype": "Int64", "nullable": False},
                {"name": "rating", "dtype": "integer", "polars_dtype": "Int64", "nullable": True},
            ],
        },
        "health": {
            "score": 96.41,
            "grade": "A",
            "components": {
                "completeness": 96.5,
                "uniqueness": 100.0,
                "consistency": 100.0,
                "cleanliness": 91.52,
            },
        },
    }


def make_findings(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "findings": [
            {
                "finding_id": str(uuid.uuid4()),
                "type": "HIGH_NULLABILITY",
                "severity": "MEDIUM",
                "confidence": 0.433,
                "column": "rating",
                "title": "High null rate in 'rating' (35.0%)",
                "description": "Column 'rating' has 35.0% missing values <html-ish & text>.",
            },
            {
                "finding_id": str(uuid.uuid4()),
                "type": "STRONG_CORRELATION",
                "severity": "LOW",
                "confidence": 0.5,
                "column": None,
                "title": "Strong correlation price ↔ revenue",
                "description": "pearson=0.97",
            },
        ],
    }


def make_insights(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "executive_summary": {
            "headline": "Sales dataset with notable revenue skew.",
            "overview": "Moderately healthy with a few data-quality gaps & <edge> cases.",
            "key_strengths": ["Schema is well-typed"],
            "key_concerns": ["Revenue has 12% outliers"],
            "recommended_next_steps": ["Investigate revenue outliers"],
        },
        "finding_explanations": [],
        "suggested_questions": [],
        "model_used": "gpt-4o",
        "degraded": False,
    }


def make_degraded_insights(dataset_id: uuid.UUID) -> dict:
    return {
        "dataset_id": str(dataset_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "executive_summary": {
            "headline": "Insights unavailable",
            "overview": "The LLM Insight layer could not generate a narrative summary.",
            "key_strengths": [],
            "key_concerns": [],
            "recommended_next_steps": [],
        },
        "finding_explanations": [],
        "suggested_questions": [],
        "model_used": "gpt-4o",
        "degraded": True,
        "degraded_reason": "OPENAI_API_KEY is not configured",
    }


@pytest.fixture
def write_artifacts(test_settings, dataset_id):
    """Callable that writes artifact JSON into the tmp data_dir."""

    def _write(*, profile=True, findings=True, insights="ok"):
        if profile:
            _write_json(
                test_settings.profiles_dir / f"{dataset_id}.json",
                make_profile(dataset_id),
            )
        if findings:
            _write_json(
                test_settings.findings_dir / f"{dataset_id}.json",
                make_findings(dataset_id),
            )
        if insights == "ok":
            _write_json(
                test_settings.llm_cache_dir / f"{dataset_id}.json",
                make_insights(dataset_id),
            )
        elif insights == "degraded":
            _write_json(
                test_settings.llm_cache_dir / f"{dataset_id}.json",
                make_degraded_insights(dataset_id),
            )
        # insights=None → no file written

    return _write


# ── app + client ─────────────────────────────────────────────────────────────

@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def app(test_settings, fake_session):
    application = create_app()

    async def _override_session():
        yield fake_session

    application.dependency_overrides[get_session] = _override_session
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.dependency_overrides[get_auth_context] = lambda: None
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
