"""Core Finding data model — the atomic unit of Layer 3."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from agentic_engine.findings.finding_types import FindingType, Severity


class Finding(BaseModel):
    finding_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    type: FindingType
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    column: str | None = None
    title: str
    description: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    semantic_context: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FindingsReport(BaseModel):
    """Top-level output of the findings layer.

    Persisted as JSON to data/findings/{dataset_id}.json and consumed by
    Layers 5 (LLM insight) and 6 (vector retrieval).
    """

    dataset_id: uuid.UUID
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    findings: list[Finding] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.findings)

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def by_type(self, finding_type: FindingType) -> list[Finding]:
        return [f for f in self.findings if f.type == finding_type]

    def for_column(self, column: str) -> list[Finding]:
        return [f for f in self.findings if f.column == column]
