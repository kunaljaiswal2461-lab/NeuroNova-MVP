"""SPEC-05 L1: the sealed query (I1) and the structural fingerprint (F10). Pure, no I/O."""
from __future__ import annotations

import copy
import dataclasses
import pickle
import uuid

import pytest

from app.services.sql_guard import (
    CatalogScope,
    Rejection,
    ScopeColumn,
    ScopeTable,
    SealedValidatedQuery,
    SealError,
    ValidatedQuery,
    is_sealed,
    seal,
    structural_fingerprint,
    validate_sql,
)
from tests.sql_guard.conftest import ROW_CAP, make_scope

SQL = "SELECT status, count(*) AS n FROM shop.orders WHERE total_amount > 100 GROUP BY status"


def scope_with_ids() -> CatalogScope:
    base = make_scope()
    return CatalogScope(
        base.source_id,
        base.snapshot_id,
        tuple(
            ScopeTable(
                t.schema, t.name,
                tuple(ScopeColumn(c.name, c.native_type, uuid.uuid4()) for c in t.columns),
                uuid.uuid4(),
            )
            for t in base.tables
        ),
    )


@pytest.fixture(scope="module")
def scope() -> CatalogScope:
    return scope_with_ids()


def make_sealed(scope: CatalogScope, sql: str = SQL) -> SealedValidatedQuery:
    validated = validate_sql(sql, scope, row_cap=ROW_CAP)
    assert isinstance(validated, ValidatedQuery), validated
    return seal(validated, scope, row_cap=ROW_CAP)


def test_seal_carries_the_validated_facts(scope):
    sealed = make_sealed(scope)
    validated = validate_sql(SQL, scope, row_cap=ROW_CAP)
    assert is_sealed(sealed)
    assert sealed.sql == validated.sql                      # I2: the regenerated text
    assert sealed.applied_limit == ROW_CAP + 1 and sealed.row_cap == ROW_CAP
    assert [(t.schema, t.name) for t in sealed.tables] == [("shop", "orders")]
    assert sorted(c.name for c in sealed.columns) == ["status", "total_amount"]
    assert len(sealed.structural_fingerprint) == 64
    int(sealed.structural_fingerprint, 16)


def test_multi_table_seal_lists_every_distinct_table_in_canonical_order(scope):
    sealed = make_sealed(
        scope, "SELECT o.id, c.city FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id"
    )
    assert [(t.schema, t.name) for t in sealed.tables] == [("shop", "customers"), ("shop", "orders")]
    assert len({t.asset_id for t in sealed.tables}) == 2


def test_a_hand_built_object_is_not_sealed(scope):
    real = make_sealed(scope)
    forged = SealedValidatedQuery(
        sql="SELECT pg_sleep(100)", structural_fingerprint=real.structural_fingerprint, tables=real.tables,
        columns=real.columns, applied_limit=1, source_id=real.source_id, snapshot_id=real.snapshot_id,
        row_cap=ROW_CAP,
    )
    assert not is_sealed(forged)


def test_dataclasses_replace_loses_the_seal(scope):
    real = make_sealed(scope)
    assert not is_sealed(dataclasses.replace(real, sql="SELECT 1"))
    assert not is_sealed(dataclasses.replace(real))         # even an identical copy


def test_a_subclass_is_not_sealed(scope):
    real = make_sealed(scope)

    class Sneaky(SealedValidatedQuery):
        pass

    sub = Sneaky(**{f.name: getattr(real, f.name) for f in dataclasses.fields(real) if f.init})
    object.__setattr__(sub, "_seal", real._seal)            # even holding the real seal
    assert not is_sealed(sub)


def test_copy_and_pickle_are_refused(scope):
    real = make_sealed(scope)
    for attempt in (copy.copy, copy.deepcopy, lambda v: pickle.loads(pickle.dumps(v))):
        with pytest.raises(TypeError):
            attempt(real)


def test_non_objects_are_not_sealed():
    for value in (None, "SELECT 1", {"sql": "SELECT 1"}, object()):
        assert not is_sealed(value)


def test_the_sealed_object_is_immutable(scope):
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_sealed(scope).sql = "SELECT 1"


def test_seal_refuses_a_tampered_validated_query(scope):
    validated = validate_sql(SQL, scope, row_cap=ROW_CAP)
    for tampered in (
        dataclasses.replace(validated, sql="SELECT pg_sleep(100) LIMIT 501"),
        dataclasses.replace(validated, applied_limit=5),
        dataclasses.replace(validated, tables=frozenset({("shop", "customers")})),
    ):
        # Text the validator would not regenerate, or facts that disagree with it, cannot be sealed.
        # (A literal-only edit is different: it is itself validated SQL, so sealing it is correct.)
        with pytest.raises(SealError):
            seal(tampered, scope, row_cap=ROW_CAP)


def test_seal_refuses_a_scope_it_was_not_validated_against(scope):
    validated = validate_sql(SQL, scope, row_cap=ROW_CAP)
    other = dataclasses.replace(scope, snapshot_id=uuid.uuid4())
    with pytest.raises(SealError):
        seal(validated, other, row_cap=ROW_CAP)


def test_seal_refuses_a_scope_without_catalog_ids():
    plain = make_scope()                                    # ids default to None
    validated = validate_sql(SQL, plain, row_cap=ROW_CAP)
    with pytest.raises(SealError):
        seal(validated, plain, row_cap=ROW_CAP)


def test_seal_refuses_a_different_row_cap(scope):
    validated = validate_sql(SQL, scope, row_cap=ROW_CAP)   # applied 501
    with pytest.raises(SealError):
        seal(validated, scope, row_cap=10)


def test_seal_refuses_something_that_is_not_a_validated_query(scope):
    rejected = validate_sql("DROP TABLE shop.orders", scope, row_cap=ROW_CAP)
    assert isinstance(rejected, Rejection)
    with pytest.raises(SealError):
        seal(rejected, scope, row_cap=ROW_CAP)              # type: ignore[arg-type]


# ── structural fingerprint (F10) ─────────────────────────────────────────────

BASE = "SELECT status FROM shop.orders WHERE total_amount > 100 AND channel = 'web' LIMIT 10"


def test_fingerprint_ignores_literal_values():
    other = "SELECT status FROM shop.orders WHERE total_amount > 9999 AND channel = 'store' LIMIT 77"
    assert structural_fingerprint(BASE) == structural_fingerprint(other)


@pytest.mark.parametrize(
    "variant",
    [
        "SELECT channel FROM shop.orders WHERE total_amount > 100 AND channel = 'web' LIMIT 10",        # column
        "SELECT status FROM shop.refunds WHERE total_amount > 100 AND channel = 'web' LIMIT 10",       # table
        "SELECT status FROM shop.orders WHERE total_amount >= 100 AND channel = 'web' LIMIT 10",       # operator
        "SELECT status FROM shop.orders WHERE total_amount > 100 AND channel = 'web'",                 # no limit
        "SELECT status FROM shop.orders WHERE total_amount > 100 OR channel = 'web' LIMIT 10",         # connective
        "SELECT status FROM shop.orders WHERE total_amount > 100 AND channel = 'web' ORDER BY 1 LIMIT 10",
    ],
)
def test_fingerprint_changes_with_shape(variant):
    assert structural_fingerprint(variant) != structural_fingerprint(BASE)


def test_fingerprint_is_64_hex_and_stable():
    first = structural_fingerprint(BASE)
    assert first == structural_fingerprint(BASE)
    assert len(first) == 64 and int(first, 16) >= 0


def test_fingerprint_never_contains_a_literal():
    # The hash is of the placeholder form; a different secret must not change it.
    a = structural_fingerprint("SELECT id FROM shop.customers WHERE email = 'alice@example.com'")
    b = structural_fingerprint("SELECT id FROM shop.customers WHERE email = 'bob@example.com'")
    assert a == b


def test_a_table_less_query_cannot_be_validated_so_cannot_be_sealed(scope):
    for sql in ("SELECT 1", "SELECT count(*) FROM generate_series(1, 10)"):
        result = validate_sql(sql, scope, row_cap=ROW_CAP)
        assert isinstance(result, Rejection) and result.code.value == "no_table_reference"


@pytest.mark.parametrize("sql", [
    "SELECT lpad(id::text, 6, '0') FROM shop.orders LIMIT 3",
    "SELECT rpad(status, 10000, 'x') FROM shop.orders LIMIT 3",          # exactly the limit
    "SELECT lpad(status, 1) FROM shop.orders LIMIT 3",
])
def test_pad_with_a_small_literal_length_is_still_accepted(scope, sql):
    assert isinstance(validate_sql(sql, scope, row_cap=ROW_CAP), ValidatedQuery)


def test_pad_rejection_names_the_limit(scope):
    result = validate_sql("SELECT lpad(status, 200000000) FROM shop.orders", scope, row_cap=ROW_CAP)
    assert result.code.value == "function_not_allowed" and "10,000" in result.message
    assert ";" not in result.message and "$" not in result.message
