"""FindingsIndex — the Layer 3 → Layer 4 bridge.

Wraps a FindingsReport and exposes fast lookup methods used by every chart
builder. This is the single place where Layer 4 reads Layer 3's output.

Responsibilities:
  skip(col)          → True if column should be excluded from charts
                        (constant, high-confidence identifier, or all-null)
  for_column(col)    → all findings referencing that column
  by_type(type)      → all findings of a given FindingType
  finding_boost(col) → (finding_ids, priority_delta) for priority adjustment
"""
from __future__ import annotations

from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.findings.models import Finding, FindingsReport

_SKIP_TYPES = {FindingType.CONSTANT_COLUMN}
_SKIP_ID_CONFIDENCE_THRESHOLD = 0.75  # only skip ID columns if classifier is confident

_HIGH_BOOST = -10
_MEDIUM_BOOST = -5


class FindingsIndex:
    def __init__(self, findings_report: FindingsReport) -> None:
        self._by_col: dict[str, list[Finding]] = {}
        self._by_type: dict[FindingType, list[Finding]] = {}
        self._skip: set[str] = set()

        for f in findings_report.findings:
            # Index by column
            if f.column:
                self._by_col.setdefault(f.column, []).append(f)

            # Index by type
            self._by_type.setdefault(f.type, []).append(f)

            # Build skip set
            if f.type == FindingType.CONSTANT_COLUMN and f.column:
                self._skip.add(f.column)
            if (
                f.type == FindingType.ID_COLUMN_DETECTED
                and f.column
                and f.confidence >= _SKIP_ID_CONFIDENCE_THRESHOLD
            ):
                self._skip.add(f.column)

    def skip(self, column: str) -> bool:
        """True → this column should be excluded from all charts."""
        return column in self._skip

    def for_column(self, column: str) -> list[Finding]:
        return self._by_col.get(column, [])

    def by_type(self, finding_type: FindingType) -> list[Finding]:
        return self._by_type.get(finding_type, [])

    def finding_boost(self, column: str) -> tuple[list[str], int]:
        """Return (finding_id_strings, priority_delta).

        Negative delta → chart sorts higher (shown earlier in the UI).
        """
        findings = self.for_column(column)
        delta = 0
        ids: list[str] = []

        for f in findings:
            ids.append(str(f.finding_id))
            if f.severity == Severity.HIGH:
                delta += _HIGH_BOOST
            elif f.severity == Severity.MEDIUM:
                delta += _MEDIUM_BOOST

        return ids, delta
