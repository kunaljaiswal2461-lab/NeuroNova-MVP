"""Findings persistence — save/load FindingsReport as JSON on disk.

Mirrors the same pattern as profiler/engine.py: large JSON blobs on disk,
small queryable metadata in PostgreSQL.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.core.config import Settings, get_settings
from agentic_engine.findings.models import FindingsReport


def findings_path(dataset_id: uuid.UUID, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.findings_dir / f"{dataset_id}.json"


def save_findings(report: FindingsReport, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.findings_dir.mkdir(parents=True, exist_ok=True)
    target = findings_path(report.dataset_id, settings)
    target.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def load_findings(
    dataset_id: uuid.UUID,
    settings: Settings | None = None,
) -> FindingsReport | None:
    target = findings_path(dataset_id, settings)
    if not target.exists():
        return None
    raw = json.loads(target.read_text(encoding="utf-8"))
    return FindingsReport.model_validate(raw)


def load_findings_raw(
    dataset_id: uuid.UUID,
    settings: Settings | None = None,
) -> dict | None:
    """Return the raw dict without deserialising — useful for API responses."""
    target = findings_path(dataset_id, settings)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
