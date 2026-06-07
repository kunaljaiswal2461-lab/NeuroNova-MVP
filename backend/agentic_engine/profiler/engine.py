"""Profiling engine entry point.

Orchestrates loader + profilers + health scorer into a ProfileReport, and
handles JSON persistence under settings.profiles_dir/{dataset_id}.json.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.dataset import FileType
from agentic_engine.profiler.health_scorer import compute_health
from agentic_engine.profiler.loader import load_dataset
from agentic_engine.profiler.quality_profiler import profile_quality
from agentic_engine.profiler.relationship_profiler import profile_relationships
from agentic_engine.profiler.report import ProfileReport
from agentic_engine.profiler.schema_profiler import profile_schema
from agentic_engine.profiler.semantic_profiler import profile_semantic
from agentic_engine.profiler.stats_profiler import profile_stats


logger = get_logger("profiler.engine")


def profile_dataset(
    *,
    dataset_id: uuid.UUID,
    raw_path: Path,
    file_type: FileType,
    settings: Settings | None = None,
) -> ProfileReport:
    settings = settings or get_settings()
    logger.info("profile.load", dataset_id=str(dataset_id), path=str(raw_path))

    df = load_dataset(raw_path, file_type)

    schema = profile_schema(df)
    stats = profile_stats(df)
    quality = profile_quality(df)
    relationships = profile_relationships(df)
    semantic = profile_semantic(df)
    health = compute_health(schema, quality, settings)

    return ProfileReport(
        dataset_id=dataset_id,
        generated_at=datetime.now(timezone.utc),
        schema=schema,
        stats=stats,
        quality=quality,
        relationships=relationships,
        semantic=semantic,
        health=health,
    )


def profile_path(dataset_id: uuid.UUID, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.profiles_dir / f"{dataset_id}.json"


def save_report(report: ProfileReport, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.profiles_dir.mkdir(parents=True, exist_ok=True)
    target = profile_path(report.dataset_id, settings)
    target.write_text(
        json.dumps(report.model_dump_json_safe(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def load_report(dataset_id: uuid.UUID, settings: Settings | None = None) -> dict | None:
    target = profile_path(dataset_id, settings)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
