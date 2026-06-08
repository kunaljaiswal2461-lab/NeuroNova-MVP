"""VizReport persistence — save/load as JSON on disk."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.core.config import Settings, get_settings
from agentic_engine.viz.models import VizReport


def viz_path(dataset_id: uuid.UUID, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.viz_dir / f"{dataset_id}.json"


def save_viz(report: VizReport, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.viz_dir.mkdir(parents=True, exist_ok=True)
    target = viz_path(report.dataset_id, settings)
    target.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def load_viz(
    dataset_id: uuid.UUID,
    settings: Settings | None = None,
) -> VizReport | None:
    target = viz_path(dataset_id, settings)
    if not target.exists():
        return None
    return VizReport.model_validate(json.loads(target.read_text(encoding="utf-8")))


def load_viz_raw(
    dataset_id: uuid.UUID,
    settings: Settings | None = None,
) -> dict | None:
    target = viz_path(dataset_id, settings)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
