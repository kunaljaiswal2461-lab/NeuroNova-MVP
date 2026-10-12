"""Golden-question schema (SPEC-03 §11.1).

Pydantic with ``extra="forbid"`` so a typo in a YAML file is an error, not a
silently ignored field.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

EVALS_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = EVALS_DIR / "golden"
MANIFEST_PATH = EVALS_DIR / "fixture" / "manifest.json"

DEFAULT_ABS_TOL = Decimal("1e-6")
DEFAULT_REL_TOL = Decimal("1e-9")
DEFAULT_MAX_EXTRA_COLUMNS = 2


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NumericTolerance(_Strict):
    abs: Decimal = DEFAULT_ABS_TOL
    rel: Decimal = DEFAULT_REL_TOL


class AcceptableAnswer(_Strict):
    reference_sql: str
    order_matters: bool = False
    max_extra_columns: int = DEFAULT_MAX_EXTRA_COLUMNS


class Expectation(_Strict):
    kind: Literal["answer", "clarification", "refusal"]
    reference_sql: str | None = None
    output_type: str | None = None
    order_matters: bool = False
    max_extra_columns: int = DEFAULT_MAX_EXTRA_COLUMNS
    numeric_tolerance: NumericTolerance = Field(default_factory=NumericTolerance)
    acceptable_answers: tuple[AcceptableAnswer, ...] = ()
    category: Literal["write", "unanswerable", "out_of_scope"] | None = None


class GoldenQuestion(_Strict):
    id: str
    question: str
    tags: tuple[str, ...]
    difficulty: Literal["easy", "medium", "hard"] | None = None
    expect: Expectation
    notes: str | None = None
    reviewed_by: str | None = None
    # SPEC-09 W6.6: the earlier exchanges of a chain. Each is {question, reference_sql} | {question, clarification} |
    # {question, failed: true}. Only this question (the final turn) is graded.
    turns: tuple[dict, ...] | None = None


class GoldenMeta(_Strict):
    fixture_version: int
    reference_clock: str
    reference_time_zone: str
    tags: tuple[str, ...]
    output_types: tuple[str, ...]


def load_meta(golden_dir: Path = GOLDEN_DIR) -> GoldenMeta:
    return GoldenMeta.model_validate(yaml.safe_load((golden_dir / "meta.yaml").read_text()))


def load_questions_raw(directory: Path) -> list[tuple[Path, dict]]:
    """Return ``(file, raw_item)`` pairs without validating (the linter reports errors)."""
    out: list[tuple[Path, dict]] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or []
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected a YAML list of questions")
        out.extend((path, item) for item in data)
    return out


def load_questions(suite: str = "golden", golden_dir: Path = GOLDEN_DIR) -> list[GoldenQuestion]:
    dirs = {
        "golden": ["questions"],
        "adversarial": ["adversarial"],
        "chains": ["chains"],                       # SPEC-09 W6.6: multi-turn, the final turn is graded
        "all": ["questions", "adversarial"],
    }[suite]
    questions: list[GoldenQuestion] = []
    for d in dirs:
        questions.extend(
            GoldenQuestion.model_validate(item)
            for _, item in load_questions_raw(golden_dir / d)
        )
    return questions
