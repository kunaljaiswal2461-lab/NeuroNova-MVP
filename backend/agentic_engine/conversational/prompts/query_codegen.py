"""Prompt for natural-language → Polars expression generation.

The output is a single Polars expression that is validated by an
AST whitelist in
:mod:`agentic_engine.conversational.query_executor` before execution.
We constrain the LLM to a known-safe surface area so AST rejection is
rare in practice (the LLM, given a tight prompt, almost always stays
inside the allowed grammar).

Hard constraints baked into the system prompt:
  * exactly one expression — no statements, no assignments;
  * only the names ``df`` and ``pl`` are in scope;
  * only read operations (filter / select / aggregate / sort / head);
  * no imports, no I/O, no attribute access starting with ``_``;
  * date literals must be expressed via ``pl.lit(...).str.to_date(...)``
    rather than ``datetime(...)`` (datetime is not in scope).
"""
from __future__ import annotations

import json
from typing import Any


SYSTEM = """You translate a user's natural-language question about a dataset into a SINGLE Polars expression.

CONTEXT
You will be given:
  - the user's question
  - the dataset schema (column name + Polars dtype + optional semantic type)
  - (optional) the most recent assistant/user turns for context

OUTPUT
Respond as JSON matching this exact schema and nothing else:
{
  "expression": "<one Polars expression evaluating to a DataFrame or scalar>",
  "explanation": "<one sentence explaining what it computes>"
}

EXPRESSION RULES (HARD CONSTRAINTS — violating any of these will cause the request to fail validation)
  1. The expression must be a SINGLE Python expression — no semicolons, no `=`, no `;`, no `import`, no `def`, no `lambda`.
  2. Only two names are in scope: `df` (the Polars DataFrame) and `pl` (the polars module).
  3. Only read-only methods are allowed. Examples of allowed methods:
       df.filter(...), df.select(...), df.sort(...), df.head(N), df.tail(N), df.limit(N),
       df.group_by(...).agg(...), df.with_columns(...),
       df.drop_nulls(...), df.unique(...), df.describe(), df.shape,
       pl.col("name"), pl.lit(value), pl.sum("..."), pl.mean("..."), pl.count(), pl.min("..."), pl.max("..."), pl.when(...).then(...).otherwise(...)
       column expression methods: .gt, .lt, .ge, .le, .eq, .ne, .is_null, .is_not_null, .is_in, .between, .alias, .cast, .str.*, .dt.*
  4. NEVER call: read_*, write_*, sink_*, to_csv, to_pandas, anything starting with `_`, anything involving `__class__`, `__globals__`, or other dunders.
  5. NEVER use imports, lambdas, comprehensions, ternaries (`x if y else z`), walrus operators, or starred unpacking.
  6. Use only column names that appear in the dataset schema. Quote them as Python strings.
  7. For comparisons with column expressions, use Python operators (`pl.col("x") > 5`) or method form (`pl.col("x").gt(5)`).
  8. Cap any "show rows" style output to at most 200 rows by chaining `.head(200)` or `.limit(200)`.
  9. Do not include markdown fences or any prose outside the JSON object."""


def build_user(
    message: str,
    schema: list[dict[str, Any]],
    *,
    history: list[dict[str, str]] | None = None,
) -> str:
    """Assemble the user-side content with the question + schema + history."""
    pieces: list[str] = []
    if history:
        pieces.append("Recent conversation (oldest first):")
        for turn in history:
            role = turn.get("role", "user").upper()
            content = turn.get("content", "").strip()
            if content:
                pieces.append(f"  [{role}] {content}")
        pieces.append("")

    pieces.append("User question:")
    pieces.append(message.strip())
    pieces.append("")
    pieces.append("Dataset schema:")
    pieces.append(json.dumps(schema, ensure_ascii=False, indent=2))
    return "\n".join(pieces)
