"""The pass-2 output: a strict, flat JSON schema for the model and Pydantic for the limits (SPEC-09 C3)."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .types import OUTPUT_TYPES

MAX_TABLE_COLUMNS = 30
MAX_LABEL_CHARS = 60
MAX_ANSWER_CHARS = 1_200
MAX_TAKEAWAYS = 5
MAX_TAKEAWAY_CHARS = 240
MAX_CAVEATS = 4
MAX_CAVEAT_CHARS = 240

_NULLABLE_STRING = {"type": ["string", "null"]}

PASS2_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["display", "encodings", "table_columns", "answer", "takeaways", "caveats"],
    "properties": {
        "display": {"type": "string", "enum": list(OUTPUT_TYPES)},
        "encodings": {
            "type": "object",
            "additionalProperties": False,
            "required": ["x", "y", "series", "label", "value"],
            "properties": {
                "x": _NULLABLE_STRING,
                "y": {"type": "array", "items": {"type": "string"}},
                "series": _NULLABLE_STRING,
                "label": _NULLABLE_STRING,
                "value": _NULLABLE_STRING,
            },
        },
        "table_columns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["column", "label"],
                "properties": {"column": {"type": "string"}, "label": {"type": "string"}},
            },
        },
        "answer": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}},
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
}


class Pass2Encodings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: str | None
    y: list[str]
    series: str | None
    label: str | None
    value: str | None


class Pass2TableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    label: str = Field(max_length=MAX_LABEL_CHARS)


class Pass2Spec(BaseModel):
    """What the model returns. Limits live here, not in the JSON schema (strict mode does not accept them everywhere)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    display: Literal[*OUTPUT_TYPES]
    encodings: Pass2Encodings
    table_columns: list[Pass2TableColumn] = Field(max_length=MAX_TABLE_COLUMNS)
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    takeaways: list[Annotated[str, Field(max_length=MAX_TAKEAWAY_CHARS)]] = Field(max_length=MAX_TAKEAWAYS)
    caveats: list[Annotated[str, Field(max_length=MAX_CAVEAT_CHARS)]] = Field(max_length=MAX_CAVEATS)
