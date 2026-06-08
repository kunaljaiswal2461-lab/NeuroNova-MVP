"""InsightReport persistence — save/load as JSON on disk.

Mirrors the same disk-for-blobs convention used by Layers 2/3/4.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.core.config import Settings, get_settings
from agentic_engine.llm_engine.models import InsightReport


def insight_path(dataset_id: uuid.UUID, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.llm_cache_dir / f"{dataset_id}.json"


def save_insights(report: InsightReport, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.llm_cache_dir.mkdir(parents=True, exist_ok=True)
    target = insight_path(report.dataset_id, settings)
    target.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def load_insights(
    dataset_id: uuid.UUID,
    settings: Settings | None = None,
) -> InsightReport | None:
    target = insight_path(dataset_id, settings)
    if not target.exists():
        return None
    return InsightReport.model_validate(json.loads(target.read_text(encoding="utf-8")))


def load_insights_raw(
    dataset_id: uuid.UUID,
    settings: Settings | None = None,
) -> dict | None:
    """Return the raw dict without deserialising — useful for API responses."""
    target = insight_path(dataset_id, settings)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
