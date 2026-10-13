"""Plain data shared by the collector, the publisher and the API (SPEC-07). No I/O and no heavy imports."""
from __future__ import annotations

from dataclasses import dataclass, field

from .select import Sample

ColumnKey = tuple[str, str, str]          # (schema, table, column)


@dataclass(frozen=True)
class SampleCollection:
    """What one sync collected, plus the fence value captured before any remote I/O.

    ``samples`` holds only kept columns; ``skips`` holds ``(schema, table, column) -> reason`` for the rest.
    Neither the skips nor anything derived from them contains a value.
    """

    generation: int
    samples: dict[ColumnKey, Sample] = field(default_factory=dict)
    skips: dict[ColumnKey, str] = field(default_factory=dict)
