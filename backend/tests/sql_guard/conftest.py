"""Scope and corpus loaders for the SQL validator tests (SPEC-04 §11.1).

The L1 scope is a hand-typed copy of the Part 0 fixture's 8 in-scope tables
(``evals/fixture/client/10_schema.sql``). T-L4 proves it equals what the real
catalog builder returns, so a drift fails the live layer rather than silently
weakening these tests.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from app.services.sql_guard.types import CatalogScope, ScopeColumn, ScopeTable
from evals.candidates.mutants import generate_mutants
from evals.models import load_questions

CORPUS_DIR = Path(__file__).parent / "corpus"
ROW_CAP = 500

_TABLES: dict[str, list[tuple[str, str]]] = {
    "regions": [("id", "int4"), ("name", "text"), ("zone", "text")],
    "customers": [
        ("id", "uuid"), ("full_name", "text"), ("email", "text"), ("phone", "text"),
        ("city", "text"), ("region_id", "int4"), ("segment", "text"), ("signup_at", "timestamptz"),
    ],
    "products": [
        ("id", "int4"), ("sku", "text"), ("name", "text"), ("category", "text"),
        ("unit_price", "numeric"), ("is_active", "bool"),
    ],
    "orders": [
        ("id", "int8"), ("customer_id", "uuid"), ("status", "text"), ("channel", "text"),
        ("ordered_at", "timestamptz"), ("delivered_on", "date"), ("total_amount", "numeric"),
    ],
    "order_items": [
        ("order_id", "int8"), ("line_no", "int4"), ("product_id", "int4"),
        ("quantity", "int4"), ("unit_price", "numeric"),
    ],
    "refunds": [
        ("id", "int8"), ("order_id", "int8"), ("amount", "numeric"),
        ("reason", "text"), ("refunded_at", "timestamp"),
    ],
    "Order Events": [
        ("id", "int8"), ("order", "int8"), ("Event Type", "text"), ("occurred_at", "timestamptz"),
    ],
    "page_views": [
        ("id", "int8"), ("customer_id", "uuid"), ("path", "text"), ("viewed_at", "timestamptz"),
    ],
}


def _table(schema: str, name: str, cols: list[tuple[str, str]]) -> ScopeTable:
    return ScopeTable(schema, name, tuple(ScopeColumn(c, t) for c, t in cols))


SOURCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


def make_scope() -> CatalogScope:
    return CatalogScope(
        SOURCE_ID, SNAPSHOT_ID,
        tuple(_table("shop", n, c) for n, c in sorted(_TABLES.items())),
    )


def make_dup_scope() -> CatalogScope:
    """Adds ``archive.orders`` so an unqualified ``orders`` is ambiguous."""
    base = make_scope()
    return CatalogScope(
        SOURCE_ID, SNAPSHOT_ID,
        base.tables + (_table("archive", "orders", _TABLES["orders"]),),
    )


def make_user_col_scope() -> CatalogScope:
    """Adds ``shop.sessions`` with columns named like SQL keywords (``user``, ``current_role``)."""
    base = make_scope()
    cols = [("id", "int8"), ("user", "text"), ("current_role", "text"), ("system_user", "text")]
    return CatalogScope(SOURCE_ID, SNAPSHOT_ID, base.tables + (_table("shop", "sessions", cols),))


SCOPES = {None: make_scope, "dup": make_dup_scope, "user_col": make_user_col_scope}


@pytest.fixture(scope="session")
def scope() -> CatalogScope:
    return make_scope()


@pytest.fixture(scope="session")
def dup_scope() -> CatalogScope:
    return make_dup_scope()


# ── corpora ─────────────────────────────────────────────────────────────────

def golden_sqls() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for q in load_questions("golden"):
        if q.expect.reference_sql:
            out.append((f"{q.id}:ref", q.expect.reference_sql))
        for i, alt in enumerate(q.expect.acceptable_answers):
            out.append((f"{q.id}:alt{i}", alt.reference_sql))
    return out


def mutant_sqls() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for q in load_questions("golden"):
        if not q.expect.reference_sql:
            continue
        for m in generate_mutants(q.expect.reference_sql, order_matters=q.expect.order_matters):
            if m.group == "equivalent" and m.sql is not None:
                out.append((f"{q.id}:{m.kind}", m.sql))
    return out


def natural_sqls() -> list[tuple[str, str]]:
    data = yaml.safe_load((CORPUS_DIR / "natural_answers.yaml").read_text())
    return [(item["id"], item["sql"]) for item in data]


def accept_corpora() -> dict[str, list[tuple[str, str]]]:
    return {"golden": golden_sqls(), "mutants": mutant_sqls(), "natural": natural_sqls()}


def all_accept_sqls() -> list[tuple[str, str]]:
    corpora = accept_corpora()
    return [(f"{name}/{cid}", sql) for name, items in corpora.items() for cid, sql in items]


def nested_parens(depth: int) -> str:
    return "SELECT " + "(" * depth + "1" + ")" * depth


def many_nodes_sql(n_terms: int = 3001) -> str:
    """A flat query with more than 3,000 AST nodes (one Literal per select item)."""
    return "SELECT " + ", ".join(["1"] * n_terms)


def yaml_reject_cases() -> list[dict]:
    return yaml.safe_load((CORPUS_DIR / "reject_cases.yaml").read_text())


def generated_reject_cases() -> list[dict]:
    return [
        {"id": "size-too-long", "sql": "SELECT '" + "a" * 20_001 + "'", "code": "input_too_large"},
        {"id": "size-33-parens", "sql": nested_parens(33), "code": "too_complex"},
        {"id": "size-2000-parens", "sql": nested_parens(2000), "code": "too_complex"},
        {"id": "size-3001-nodes", "sql": many_nodes_sql(), "code": "too_complex"},
        {
            "id": "size-31-tables",
            "sql": "SELECT 1 FROM " + ", ".join(f"shop.orders t{i}" for i in range(31)),
            "code": "too_complex",
        },
    ]


def all_reject_cases() -> list[dict]:
    return yaml_reject_cases() + generated_reject_cases()
