"""Deterministic Polars API-drift normalisation for generated queries.

The LLM regularly produces expressions that are *semantically* correct
but use pre-1.0 Polars spellings (``df.sort("x", reverse=True)``,
``df.groupby(...)``, ``pl.col("x").between(...)``, ``pl.count()``).
Polars 1.x rejects those at runtime with a ``TypeError`` or
``AttributeError``, which previously surfaced to the user as a failed
query even though the intent was perfectly executable.

This module rewrites those known renames at the AST level *before*
validation. It is intentionally narrow:

  * only pure renames with identical semantics are rewritten;
  * the rewrite operates on the parsed tree, never on the source string,
    so it cannot be tricked by column names that happen to contain
    ``reverse`` or ``groupby``;
  * every rewrite is recorded so the executor can log it and the UI can
    show the expression that actually ran.

The output is still subject to the full AST whitelist — normalisation
widens nothing security-wise, it only translates vocabulary.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

from agentic_engine.conversational.query_allowlist import (
    LEGACY_KEYWORD_RENAMES,
    LEGACY_METHOD_RENAMES,
    LEGACY_PL_FUNCTION_RENAMES,
)


@dataclass
class NormalisedExpression:
    """Result of :func:`normalise_expression`."""

    expression: str
    rewrites: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.rewrites)


class _LegacyApiRewriter(ast.NodeTransformer):
    def __init__(self) -> None:
        self.rewrites: list[str] = []

    # ``obj.legacy_method(...)`` → ``obj.current_method(...)`` and
    # ``pl.count()`` → ``pl.len()``.
    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "pl":
            # ``pl.<fn>`` renames are handled in :meth:`visit_Call` because
            # they need to see the call's arguments.
            return node
        replacement = LEGACY_METHOD_RENAMES.get(node.attr)
        if replacement is not None:
            self.rewrites.append(f".{node.attr} -> .{replacement}")
            node.attr = replacement
        return node

    # ``.sort(..., reverse=True)`` → ``.sort(..., descending=True)``.
    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        # ``pl.count()`` → ``pl.len().alias("count")``. The alias keeps the
        # output column name identical so any downstream reference to
        # ``pl.col("count")`` in the same expression keeps working.
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "pl"
            and func.attr in LEGACY_PL_FUNCTION_RENAMES
            and not node.args
            and not node.keywords
        ):
            replacement = LEGACY_PL_FUNCTION_RENAMES[func.attr]
            self.rewrites.append(
                f"pl.{func.attr}() -> pl.{replacement}().alias({func.attr!r})"
            )
            renamed = ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="pl", ctx=ast.Load()),
                    attr=replacement,
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            )
            return ast.Call(
                func=ast.Attribute(value=renamed, attr="alias", ctx=ast.Load()),
                args=[ast.Constant(value=func.attr)],
                keywords=[],
            )
        if isinstance(node.func, ast.Attribute):
            keyword_map = LEGACY_KEYWORD_RENAMES.get(node.func.attr)
            if keyword_map:
                for keyword in node.keywords:
                    replacement = keyword_map.get(keyword.arg or "")
                    if replacement is not None:
                        self.rewrites.append(
                            f".{node.func.attr}({keyword.arg}=) -> "
                            f".{node.func.attr}({replacement}=)"
                        )
                        keyword.arg = replacement
        return node


def normalise_expression(expression: str) -> NormalisedExpression:
    """Rewrite known legacy Polars spellings to their 1.x equivalents.

    Returns the original expression untouched (and ``rewrites == []``)
    when the source does not parse; the validator downstream produces
    the user-facing syntax error in that case.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return NormalisedExpression(expression=expression)

    rewriter = _LegacyApiRewriter()
    tree = rewriter.visit(tree)
    if not rewriter.rewrites:
        return NormalisedExpression(expression=expression)

    ast.fix_missing_locations(tree)
    return NormalisedExpression(
        expression=ast.unparse(tree),
        rewrites=rewriter.rewrites,
    )
