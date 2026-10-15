"""``chat_messages.nl_sql``: what a SQL-path assistant turn stores (SPEC-09 C2). SQL lives here and nowhere else (I14).

Rows are not part of this record; they stay in ``postgres_result``. ``masked_columns`` holds labels only: never
values and never pseudonyms.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_CONFIG = ConfigDict(extra="forbid")

RECORD_VERSION = 2          # v2 (SPEC-10 W3) adds segments and display_time_zone; v1 rows still load
ROUTES = ("sql", "sql_after_metric_fallthrough")


class RepairModel(BaseModel):
    model_config = _CONFIG

    reason_code: str
    original_sql: str
    attempts: int = 1


class Pass2Info(BaseModel):
    model_config = _CONFIG

    consent: bool
    masked_columns: list[str] = Field(default_factory=list)
    rows_sent: int = 0
    budget_cut: Literal["rows_20", "rows_5", "stats_only"] | None = None


class TableColumn(BaseModel):
    model_config = _CONFIG

    column: str
    label: str


class Segment(BaseModel):
    """One piece of resolved prose: literal text, or a value the server inserted (SPEC-10 D2)."""

    model_config = _CONFIG

    t: Literal["text", "value"]
    text: str | None = Field(default=None, max_length=2_000)
    exact: str | None = Field(default=None, max_length=400)
    kind: str | None = None
    source: Literal["row", "stat", "rowcount", "delta", "delta_pct"] | None = None
    column: str | None = None


class PresentationRecord(BaseModel):
    """The resolved presentation spec. ``answer``/``takeaways`` are resolved text; the templates keep the references."""

    model_config = _CONFIG

    display: str
    encodings: dict[str, Any]
    table_columns: list[TableColumn] = Field(default_factory=list)
    answer_template: str
    answer: str
    takeaways_template: list[str] = Field(default_factory=list)
    takeaways: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    fallback: str | None = None
    # Numbers the server computed for the display (kpi_comparison: delta and delta_pct). The browser never computes them.
    derived: dict[str, str] = Field(default_factory=dict)
    pass2: Pass2Info | None = None
    # SPEC-10 W3: where the values are, so the browser can format them without re-parsing prose.
    answer_segments: list[Segment] = Field(default_factory=list)
    takeaways_segments: list[list[Segment]] = Field(default_factory=list)
    display_time_zone: str | None = None


class LLMUsage(BaseModel):
    model_config = _CONFIG

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class NlSqlTurn(BaseModel):
    model_config = _CONFIG

    version: int = RECORD_VERSION
    route: Literal["sql", "sql_after_metric_fallthrough"] = "sql"
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    sql: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    output_intent: dict[str, Any] | None = None
    rewrites: list[str] = Field(default_factory=list)
    intent_warnings: list[str] = Field(default_factory=list)
    repair: RepairModel | None = None
    outcome: Literal["answer", "clarification", "unsupported", "failed"]
    failure_code: str | None = None
    presentation: PresentationRecord | None = None
    llm: LLMUsage = Field(default_factory=LLMUsage)
