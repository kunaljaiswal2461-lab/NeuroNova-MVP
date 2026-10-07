"""Shared generators for the validator's fuzz tests (SPEC-04 T-U6, T-L2). Not a test module."""
from __future__ import annotations

from hypothesis import strategies as st
from sqlglot.dialects.dialect import Dialect

from .conftest import all_accept_sqls

BASES = [sql for _, sql in all_accept_sqls()[::7]]

VOCAB = [
    "SELECT", "FROM", "WHERE", "JOIN", "UNION", "WITH", "LIMIT", "OFFSET", "FETCH", "FOR UPDATE", "INTO",
    "GROUP BY", "ORDER BY", "HAVING", "OVER", "FILTER", "WITHIN GROUP", "LATERAL", "RECURSIVE", "AS",
    "ANY", "ALL", "ARRAY", "VALUES", "TABLESAMPLE",
    "count(", "sum(", "date_trunc(", "generate_series(", "coalesce(", "now()",
    "pg_sleep(1)", "pg_catalog.pg_sleep(1)", "dblink(", "version()", "current_user", "set_config(",
    "pg_read_file(", "random()", "md5(", "'x'::regclass", "::oid", "::json", "$1", "?",
    "shop.orders", "orders", "pg_class", "internal.salaries", "information_schema.tables", "nn_client.shop.orders",
    "'", '"', "''", ";", "--", "/*", "*/", "$$", "$t$", "U&", 'U&"\\0070g_sleep"', "E'\\x41'", "\\",
    "(", ")", "((", "))", ",", ".", "*", "=", "<", ">", "+", "&", "||", "\x00", "é", "1", "0", "-1", "99999999999",
]


# Catalog-shaped tokens: edits that stay parseable and so reach the table / column / function checks.
BENIGN = [
    "shop.orders", "shop.customers", "orders", "customers", "pg_class", "internal.salaries", "shop.audit_log",
    "status", "region", "total_amount", "customer_id", "id", "o.id", "ordered_at", "count(*)", "count(id)",
    "pg_sleep(1)", "version()", "current_user", "random()", "now()", "lower(status)", "date_trunc('month', ordered_at)",
    "'x'::regclass", "id::oid", "id::text", "id::json", "1", "0", "'a'", "NULL", "LIMIT 5", "LIMIT 100000", "OFFSET 2",
    "WHERE id > 5", "ORDER BY 1", "GROUP BY 1", "FOR UPDATE", "INTO t", "ANY (ARRAY[1])", "AS x", "AS pg_sleep",
]
MIXED_VOCAB = BENIGN * 3 + VOCAB


def splice(base: str, edits: list[tuple[int, str]]) -> str:
    """Character-level splice: breaks tokens, so it mostly probes the parse-error paths."""
    out = base
    for pos, token in edits:
        i = pos % (len(out) + 1)
        out = out[:i] + " " + token + " " + out[i:]
    return out


def token_pieces(sql: str) -> list[str]:
    return [sql[t.start:t.end + 1] for t in Dialect.get_or_raise("postgres").tokenize(sql)]


PIECES = {base: token_pieces(base) for base in BASES}


def splice_tokens(base: str, edits: list[tuple[str, int, str]]) -> str:
    """Token-level edits (insert / replace / delete / duplicate) keep most queries parseable,
    so many examples reach the table, column, function and cast checks and get accepted."""
    pieces = list(PIECES[base])
    for kind, pos, token in edits:
        i = pos % (len(pieces) + 1)
        if kind == "insert":
            pieces.insert(i, token)
        elif pieces and kind == "replace":
            pieces[i % len(pieces)] = token
        elif pieces and kind == "delete":
            del pieces[i % len(pieces)]
        elif pieces and kind == "duplicate":
            pieces.insert(i, pieces[i % len(pieces)])
    return " ".join(pieces)


char_edits = st.lists(st.tuples(st.integers(0, 10_000), st.sampled_from(MIXED_VOCAB)), min_size=1, max_size=4)
token_edits = st.lists(
    st.tuples(st.sampled_from(["insert", "replace", "delete", "duplicate"]), st.integers(0, 10_000), st.sampled_from(VOCAB)),
    min_size=1, max_size=3,
)


TABLE_NAMES = [
    "shop.orders", "orders", "shop.customers", "pg_catalog.pg_authid", "pg_class", "information_schema.tables",
    "internal.salaries", "shop.audit_log", 'shop."ORDERS"', "nn_client.shop.orders", "shop.\"Order Events\"",
]
SNIPPETS = [
    "pg_sleep(1)", "pg_catalog.pg_sleep(1)", "version()", "current_user", "user", "random()", "md5(status)",
    "'pg_authid'::regclass", "1::oid", "status::json", "status::text", "(SELECT 1 FROM pg_roles LIMIT 1)",
    "(SELECT count(*) FROM shop.orders)", "(SELECT id FROM internal.salaries)", "lower(status)", "now()",
    "count(*)", "coalesce(status, 'x')", "CASE WHEN id > 1 THEN 1 ELSE 0 END", "ARRAY[1,2]", "id & 1",
    "date_trunc('month', ordered_at)", "set_config('a','b',false)", "current_setting('x')", "$1", "1", "'a'", "NULL",
    "status", "region", "o.id", "id", "total_amount",
]
TAILS = [
    "", " LIMIT 5", " LIMIT 100000", " LIMIT ALL", " OFFSET 3", " FOR UPDATE", " UNION SELECT 1 FROM pg_roles",
    " UNION ALL SELECT id FROM shop.orders", " LIMIT (SELECT 5)", " FETCH FIRST 3 ROWS ONLY", "; SELECT 1",
    " ORDER BY 1 LIMIT 10 OFFSET 2",
]
PREFIXES = ["", "", "", "", "EXPLAIN ", "WITH w AS (SELECT 1 AS a) ", "WITH pg_class AS (SELECT 1 AS a) "]
tree_ops = st.lists(
    st.tuples(st.sampled_from(["table", "expr", "expr"]), st.integers(0, 10_000), st.integers(0, 10_000)),
    min_size=1, max_size=3,
)


def mutate_tree(base: str, ops: list[tuple[str, int, int]], tail: str, prefix: str) -> str:
    """Grammar-aware mutation: swap table references and expressions for hostile ones, then add a tail."""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(base, read="postgres")
    for kind, pick, choice in ops:
        if kind == "table":
            nodes = list(tree.find_all(exp.Table))
            if nodes:
                nodes[pick % len(nodes)].replace(exp.to_table(TABLE_NAMES[choice % len(TABLE_NAMES)], dialect="postgres"))
        else:
            nodes = [n for n in tree.walk() if isinstance(n, (exp.Column, exp.Literal)) and n.parent is not None]
            if nodes:
                try:
                    nodes[pick % len(nodes)].replace(sqlglot.parse_one(SNIPPETS[choice % len(SNIPPETS)], read="postgres"))
                except Exception:  # noqa: BLE001 - a snippet that does not parse is simply a skipped edit
                    pass
    return prefix + tree.sql(dialect="postgres") + tail
