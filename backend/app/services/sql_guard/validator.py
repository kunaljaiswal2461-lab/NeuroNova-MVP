"""The SQL validator (SPEC-04 §7). Pure: no I/O, no clock, no randomness (INV-V8).

``validate_sql`` parses once, checks the tree, rewrites it in exactly two ways
(schema-qualify unqualified catalog tables; set the top-level LIMIT), and returns
the SQL *regenerated from the validated tree*. Only that text may be executed
(INV-V1). The regenerated text is then re-validated and must be a fixed point
(INV-V5), which also puts the final text, not the model's, through the token layer.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema
from sqlglot.tokens import Token, TokenType

from .allowlist import (
    ALLOWED_FUNCTIONS,
    ALLOWED_NODE_TYPES,
    ALLOWED_TABLE_FUNCTION_NODES,
    ALLOWED_TYPE_NAMES,
    NODE_DESCRIPTIONS,
    ROOT_TYPES,
    SQL_VALUE_KEYWORDS,
)
from .messages import reject, suggest, table_pairs
from .types import CatalogScope, GuardLimits, Rejection, RejectionCode, ValidatedQuery

logger = logging.getLogger(__name__)

_DIALECT = "postgres"
MAX_PAD_LENGTH = 10_000


class _SqlglotQuiet(logging.Filter):
    """sqlglot logs the full statement when it falls back to ``Command``; keep SQL out of logs (INV-V6)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "unsupported syntax" not in record.getMessage()


logging.getLogger("sqlglot").addFilter(_SqlglotQuiet())


class _Stop(Exception):
    """Carries a Rejection out of a stage."""

    def __init__(self, rejection: Rejection):
        self.rejection = rejection


def _stop(*args, **kwargs) -> _Stop:
    return _Stop(reject(*args, **kwargs))


@dataclass(frozen=True)
class _Checked:
    tree: exp.Expression
    tables: frozenset[tuple[str, str]]
    columns: frozenset[tuple[str, str, str]]
    functions: frozenset[str]
    model_limit: int | None
    applied_limit: int


# ── public entry ────────────────────────────────────────────────────────────

def validate_sql(
    sql: str,
    scope: CatalogScope,
    *,
    row_cap: int,
    limits: GuardLimits | None = None,
) -> ValidatedQuery | Rejection:
    """Validate ``sql`` against ``scope``. Never raises (INV-V6)."""
    limits = limits or GuardLimits()
    try:
        return _validate(sql, scope, row_cap, limits)
    except _Stop as stop:
        return stop.rejection
    except RecursionError:
        return reject(RejectionCode.TOO_COMPLEX)
    except Exception as exc:  # noqa: BLE001 - fail closed; never log SQL text
        logger.warning("sql_guard internal error: %s", type(exc).__name__)
        return reject(RejectionCode.INTERNAL_ERROR)


def _validate(sql: str, scope: CatalogScope, row_cap: int, limits: GuardLimits) -> ValidatedQuery:
    first = _check(sql, scope, row_cap, limits)
    regenerated = _render(first.tree)
    try:
        second = _check(regenerated, scope, row_cap, limits)
    except _Stop:
        # The text we would execute failed our own checks: fail closed (INV-V5).
        raise _stop(RejectionCode.INTERNAL_ERROR) from None
    if (
        _render(second.tree) != regenerated
        or second.tables != first.tables
        or second.columns != first.columns
        or second.applied_limit != first.applied_limit
    ):
        raise _stop(RejectionCode.INTERNAL_ERROR)
    return ValidatedQuery(
        sql=regenerated,
        tables=second.tables,
        columns=second.columns,
        functions=second.functions,
        model_limit=first.model_limit,
        applied_limit=first.applied_limit,
        fingerprint=hashlib.sha256(regenerated.encode("utf-8")).hexdigest(),
        source_id=scope.source_id,
        snapshot_id=scope.snapshot_id,
    )


def _render(tree: exp.Expression) -> str:
    # Comments are dropped: Postgres has no use for them and they are a channel into the executed text.
    return tree.sql(dialect=_DIALECT, comments=False)


def _check(sql: str, scope: CatalogScope, row_cap: int, limits: GuardLimits) -> _Checked:
    _check_raw_text(sql, limits)
    tokens = _tokenize(sql, limits)
    tree = _parse(sql)
    _check_root_and_size(tree, limits)
    _check_nodes(tree)
    functions = _check_function_tokens(tokens, tree)
    tables, referenced = _check_tables(tree, scope, limits)
    columns = _check_columns(tree, scope, referenced)
    _check_cast_types(tree)
    if not tables:
        # SPEC-05 decision: a query that reads no catalog table (``SELECT 1``, bare ``generate_series``)
        # has nothing to authorize or lock and is not an answer about the user's data. Checked after the
        # column and function stages so those keep their more specific codes.
        raise _stop(RejectionCode.NO_TABLE_REFERENCE)
    model_limit, applied_limit = _apply_row_cap(tree, row_cap)
    return _Checked(tree, tables, columns, functions, model_limit, applied_limit)


# ── stage 1: raw text ───────────────────────────────────────────────────────

def _check_raw_text(sql: str, limits: GuardLimits) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise _stop(RejectionCode.EMPTY_INPUT)
    if len(sql) > limits.max_chars:
        raise _stop(RejectionCode.INPUT_TOO_LARGE, max_chars=limits.max_chars)
    if "\x00" in sql:
        raise _stop(RejectionCode.INVALID_TEXT)


# ── stage 2: tokens ─────────────────────────────────────────────────────────

def _tokenize(sql: str, limits: GuardLimits) -> list[Token]:
    try:
        tokens = Dialect.get_or_raise(_DIALECT).tokenize(sql)
    except (TokenError, ParseError):
        raise _stop(RejectionCode.PARSE_ERROR) from None
    depth = deepest = 0
    for token in tokens:
        if token.token_type is TokenType.L_PAREN:
            depth += 1
            deepest = max(deepest, depth)
        elif token.token_type is TokenType.R_PAREN:
            depth = max(depth - 1, 0)
        elif token.token_type in (TokenType.PLACEHOLDER, TokenType.PARAMETER):
            raise _stop(RejectionCode.UNSUPPORTED_SYNTAX, detail="Query parameters are not supported.")
    if deepest > limits.max_paren_depth:
        raise _stop(RejectionCode.TOO_COMPLEX)
    return tokens


# ── stage 3: parse ──────────────────────────────────────────────────────────

def _parse(sql: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read=_DIALECT, error_level=sqlglot.ErrorLevel.RAISE)
    except (ParseError, TokenError):
        raise _stop(RejectionCode.PARSE_ERROR) from None
    except RecursionError:
        raise _stop(RejectionCode.TOO_COMPLEX) from None
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise _stop(RejectionCode.MULTIPLE_STATEMENTS)
    return statements[0]


# ── stage 4: root and size ──────────────────────────────────────────────────

def _check_root_and_size(tree: exp.Expression, limits: GuardLimits) -> None:
    if not isinstance(tree, ROOT_TYPES):
        raise _stop(RejectionCode.UNSUPPORTED_STATEMENT)
    count = 0
    for _ in tree.walk():
        count += 1
        if count > limits.max_ast_nodes:
            raise _stop(RejectionCode.TOO_COMPLEX)
    for with_ in tree.find_all(exp.With):
        if with_.args.get("recursive"):
            raise _stop(RejectionCode.UNSUPPORTED_SYNTAX, detail="Recursive CTEs are not supported.")


# ── stage 5: node types ─────────────────────────────────────────────────────

def _is_plain_int(node: exp.Expression | None) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit()


def _check_nodes(tree: exp.Expression) -> None:
    for node in tree.walk():
        name = type(node).__name__
        if isinstance(node, exp.Dot):
            if isinstance(node.expression, exp.Func):
                raise _stop(RejectionCode.SCHEMA_QUALIFIED_FUNCTION)
            raise _stop(RejectionCode.UNSUPPORTED_SYNTAX, detail="This qualified expression is not supported.")
        if (
            isinstance(node, exp.Column)
            and not node.table
            and isinstance(node.this, exp.Identifier)
            and not node.this.quoted
            and node.name.lower() in SQL_VALUE_KEYWORDS
        ):
            raise _stop(
                RejectionCode.UNSUPPORTED_SYNTAX,
                detail="A reserved SQL keyword cannot be used as a column name unless it is double-quoted.",
            )
        if isinstance(node, (exp.From, exp.Join)) and isinstance(node.this, (exp.Select, exp.SetOperation)):
            # `FROM FROM t` / `FROM SELECT ...`: a bare subquery that Postgres would not parse the same way.
            raise _stop(RejectionCode.UNSUPPORTED_SYNTAX, detail="Wrap a subquery in parentheses.")
        if isinstance(node, exp.ObjectIdentifier):
            # `x::regclass` / `CAST(x AS regoper)`: sqlglot gives these their own node, not a DataType.
            raise _stop(RejectionCode.CAST_TYPE_NOT_ALLOWED, identifier=node.name)
        if isinstance(node, exp.LimitOptions) and isinstance(node.parent, exp.Fetch):
            continue                                   # validated with its Fetch below
        if name not in ALLOWED_NODE_TYPES:
            description = NODE_DESCRIPTIONS.get(name, f"{name} syntax")
            raise _stop(RejectionCode.UNSUPPORTED_SYNTAX, detail=f"'{description}' is not allowed.")
        if isinstance(node, exp.Fetch):
            options = node.args.get("limit_options")
            if (
                not _is_plain_int(node.args.get("count"))
                or (options is not None and (options.args.get("percent") or options.args.get("with_ties")))
            ):
                raise _stop(
                    RejectionCode.UNSUPPORTED_SYNTAX,
                    detail="Use LIMIT n or FETCH FIRST n ROWS ONLY with a plain number.",
                )
        if isinstance(node, exp.Pad) and not (
            _is_plain_int(node.expression) and int(node.expression.name) <= MAX_PAD_LENGTH
        ):
            # lpad/rpad can build a huge string from a tiny query; its plan cost is small, so the cost
            # gate cannot see it, and the whole value would be pulled into memory by the driver.
            raise _stop(
                RejectionCode.FUNCTION_NOT_ALLOWED,
                identifier="rpad" if node.args.get("is_left") is not True else "lpad",
                detail=f"The length argument of lpad and rpad must be a whole-number literal of at most {MAX_PAD_LENGTH:,}.",
            )
        if isinstance(node, exp.Anonymous) and str(node.name).lower() in {"lpad", "rpad"}:
            # A shape sqlglot did not map to Pad (e.g. a different argument count): never trust its length.
            raise _stop(
                RejectionCode.FUNCTION_NOT_ALLOWED,
                identifier=node.name,
                detail=f"The length argument of lpad and rpad must be a whole-number literal of at most {MAX_PAD_LENGTH:,}.",
            )
        if isinstance(node, exp.Anonymous) and str(node.name).lower() not in ALLOWED_FUNCTIONS:
            raise _stop(RejectionCode.FUNCTION_NOT_ALLOWED, identifier=node.name)


# ── stage 6: function names, as Postgres will read them ─────────────────────

def _declared_names(tree: exp.Expression) -> set[str]:
    """Names the query itself declares (CTEs and aliases): ``AS g(x)`` looks like ``g(``."""
    names: set[str] = set()
    for node in tree.walk():
        if isinstance(node, exp.CTE):
            names.add(node.alias.lower())
        elif isinstance(node, exp.TableAlias):
            names.add(node.name.lower())
        elif isinstance(node, exp.Alias):
            names.add(node.alias.lower())
    names.discard("")
    return names


def _check_function_tokens(tokens: list[Token], tree: exp.Expression) -> frozenset[str]:
    declared = _declared_names(tree)
    used: set[str] = set()
    for i, token in enumerate(tokens[:-1]):
        following = tokens[i + 1]
        if token.token_type is TokenType.VAR and token.text.upper() == "ROWS" and following.token_type is TokenType.FROM:
            raise _stop(RejectionCode.TABLE_FUNCTION_NOT_ALLOWED)
        if following.token_type is not TokenType.L_PAREN:
            continue
        low = token.text.lower()
        if token.token_type is TokenType.IDENTIFIER:
            if low in declared:
                continue                                # a quoted alias column list: "t"(a, b)
            raise _stop(RejectionCode.FUNCTION_NOT_ALLOWED, identifier=token.text)
        if token.token_type is not TokenType.VAR:
            if low in ALLOWED_FUNCTIONS:
                used.add(low)                           # keyword-typed names such as LEFT( / REPLACE(
            continue
        if low in declared:
            continue
        if low == "group" and i > 0 and tokens[i - 1].text.upper() == "WITHIN":
            continue                                    # WITHIN GROUP (ORDER BY ...): structural, not a call
        if low not in ALLOWED_FUNCTIONS:
            raise _stop(RejectionCode.FUNCTION_NOT_ALLOWED, identifier=token.text)
        used.add(low)
    return frozenset(used)


# ── stage 7: tables ─────────────────────────────────────────────────────────

_SIMPLE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _effective(identifier: exp.Identifier) -> str:
    """Postgres folding: unquoted identifiers are lowercased, quoted ones keep their case."""
    return identifier.name if identifier.quoted else identifier.name.lower()


def _visible_cte_names(table: exp.Table) -> set[str]:
    """CTE names a table reference can see: those of every enclosing WITH, earlier definitions only
    when the reference sits inside another CTE's body. (Recursive CTEs are rejected earlier.)"""
    names: set[str] = set()
    child: exp.Expression = table
    parent = table.parent
    while parent is not None:
        with_ = parent.args.get("with_")
        if isinstance(with_, exp.With):
            if child is with_:
                for cte in with_.expressions:
                    if _is_inside(cte, table):
                        break
                    names.add(cte.alias.lower())
            else:
                names.update(cte.alias.lower() for cte in with_.expressions)
        child, parent = parent, parent.parent
    return names


def _is_inside(container: exp.Expression, node: exp.Expression) -> bool:
    ancestor = node.parent
    while ancestor is not None:
        if ancestor is container:
            return True
        ancestor = ancestor.parent
    return False


def _qualified_name(table: exp.Table) -> str:
    return ".".join(part.name for part in (table.args.get("catalog"), table.args.get("db"), table.this) if part is not None)


def _schema_identifier(schema: str) -> exp.Identifier:
    return exp.Identifier(this=schema, quoted=not _SIMPLE_IDENTIFIER.match(schema))


def _check_tables(
    tree: exp.Expression, scope: CatalogScope, limits: GuardLimits
) -> tuple[frozenset[tuple[str, str]], list]:
    referenced = []                                    # ScopeTable objects, one per catalog reference
    references = 0
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier):
            if type(table.this).__name__ in ALLOWED_TABLE_FUNCTION_NODES:
                continue
            raise _stop(RejectionCode.TABLE_FUNCTION_NOT_ALLOWED)
        name = _effective(table.this)
        db = table.args.get("db")
        catalog = table.args.get("catalog")
        if any(part is not None and not isinstance(part, exp.Identifier) for part in (db, catalog)):
            raise _stop(RejectionCode.PARSE_ERROR)           # `shop..orders`: an empty name part
        if catalog is not None:
            raise _stop(RejectionCode.DATABASE_QUALIFIED_TABLE, identifier=_qualified_name(table))
        if db is None and name in _visible_cte_names(table):
            continue                                   # a CTE defined in this query
        references += 1
        if references > limits.max_table_refs:
            raise _stop(RejectionCode.TOO_COMPLEX)
        matches = scope.find(_effective(db) if db is not None else None, name)
        if not matches:
            raise _stop(
                RejectionCode.TABLE_NOT_IN_SCOPE,
                identifier=_qualified_name(table),
                suggestions=suggest(name, table_pairs(scope)),
            )
        if len(matches) > 1:
            raise _stop(RejectionCode.AMBIGUOUS_TABLE, identifier=name)
        match = matches[0]
        if db is None:
            table.set("db", _schema_identifier(match.schema))   # rewrite 1 of 2: always schema-qualified
        referenced.append(match)
    return frozenset((t.schema, t.name) for t in referenced), referenced


# ── stage 8: columns ────────────────────────────────────────────────────────

_UNRESOLVED = re.compile(r"Column '(.+?)' could not be resolved|Unknown column: (.+)")


def _mapping_schema(scope: CatalogScope) -> MappingSchema:
    nested: dict[str, dict[str, dict[str, str]]] = {}
    for table in scope.tables:
        nested.setdefault(table.schema, {})[table.name] = {c.name: _safe_type(c.native_type) for c in table.columns}
    return MappingSchema(nested, dialect=_DIALECT, normalize=False)


def _safe_type(native_type: str) -> str:
    try:
        exp.DataType.build(native_type, dialect=_DIALECT, udt=True)
        return native_type
    except Exception:  # noqa: BLE001 - an unparseable catalog type only loses type inference
        return "text"


def _check_columns(tree: exp.Expression, scope: CatalogScope, referenced: list) -> frozenset[tuple[str, str, str]]:
    schema = _mapping_schema(scope)
    try:
        qualified = qualify(
            tree.copy(),
            schema=schema,
            dialect=_DIALECT,
            validate_qualify_columns=True,
            quote_identifiers=True,
            identify=False,
        )
    except OptimizeError as exc:
        raise _column_rejection(str(exc), tree, schema, referenced) from None
    columns: set[tuple[str, str, str]] = set()
    for sc in traverse_scope(qualified):
        for column in sc.columns:
            source = sc.sources.get(column.table)
            if isinstance(source, exp.Table) and source.args.get("db") is not None and isinstance(source.this, exp.Identifier):
                columns.add((source.args["db"].name, source.this.name, column.name))
    return frozenset(columns)


def _column_rejection(message: str, tree: exp.Expression, schema: MappingSchema, referenced: list) -> _Stop:
    found = _UNRESOLVED.search(message)
    if not found:
        return _stop(RejectionCode.UNSUPPORTED_SYNTAX, detail="This query could not be resolved against the catalog.")
    column = found.group(1) or found.group(2)
    if "for table" not in message and _is_ambiguous(column, tree, schema):
        return _stop(RejectionCode.AMBIGUOUS_COLUMN, identifier=column)
    seen: dict[tuple[str, str], object] = {(t.schema, t.name): t for t in referenced}
    pairs = [(f"{t.name}.{c.name}", c.name) for t in seen.values() for c in t.columns]
    return _stop(RejectionCode.COLUMN_NOT_FOUND, identifier=column, suggestions=suggest(column, pairs))


def _is_ambiguous(column: str, tree: exp.Expression, schema: MappingSchema) -> bool:
    """sqlglot reports an ambiguous column with the same message as a missing one; ask each scope."""
    for sc in traverse_scope(tree):
        for node in sc.columns:
            if node.table or _effective(node.this) != column:
                continue
            suppliers = 0
            for source in sc.sources.values():
                if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
                    try:
                        names = schema.column_names(source, only_visible=False)
                    except Exception:  # noqa: BLE001
                        names = []
                    suppliers += column in names
                elif hasattr(source, "expression"):
                    suppliers += column.lower() in {n.lower() for n in source.expression.named_selects}
            if suppliers > 1:
                return True
    return False


# ── stage 9: cast types and row cap ─────────────────────────────────────────

def _check_cast_types(tree: exp.Expression) -> None:
    for node in tree.find_all(exp.DataType):
        if node.this.name not in ALLOWED_TYPE_NAMES:
            raise _stop(RejectionCode.CAST_TYPE_NOT_ALLOWED, identifier=node.sql(dialect=_DIALECT).lower())


def _literal_int(node: exp.Expression | None) -> int:
    if not _is_plain_int(node):
        raise _stop(RejectionCode.NON_LITERAL_LIMIT)
    return int(node.name)


def _apply_row_cap(tree: exp.Expression, row_cap: int) -> tuple[int | None, int]:
    """Rewrite 2 of 2. Keeps the model's LIMIT when it is within the cap, else fetches cap + 1."""
    limit = tree.args.get("limit")
    model_limit: int | None = None
    if isinstance(limit, exp.Fetch):
        model_limit = _literal_int(limit.args.get("count"))
    elif isinstance(limit, exp.Limit):
        model_limit = _literal_int(limit.expression)
        if limit.args.get("offset") is not None:
            _literal_int(limit.args["offset"])
    elif limit is not None:
        raise _stop(RejectionCode.NON_LITERAL_LIMIT)
    offset = tree.args.get("offset")
    if offset is not None:
        _literal_int(offset.expression)
    applied = model_limit if model_limit is not None and model_limit <= row_cap else row_cap + 1
    tree.set("limit", exp.Limit(expression=exp.Literal.number(applied)))
    return model_limit, applied
