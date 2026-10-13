"""T-U6: Hypothesis robustness. SPEC-04 §11.3."""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st

from app.services.sql_guard import validate_sql
from app.services.sql_guard.allowlist import ALLOWED_FUNCTIONS
from app.services.sql_guard.types import Rejection, ValidatedQuery

from .conftest import ROW_CAP, make_scope
from .fuzz import BASES, MIXED_VOCAB, PREFIXES, TAILS, char_edits, mutate_tree, splice, splice_tokens, token_edits, tree_ops

SCOPE = make_scope()
IN_SCOPE = {(t.schema, t.name) for t in SCOPE.tables}

def check_invariants(sql: str) -> None:
    result = validate_sql(sql, SCOPE, row_cap=ROW_CAP)       # INV-V6: nothing escapes
    if isinstance(result, Rejection):
        assert "$" not in result.message and ";" not in result.message
        return
    assert isinstance(result, ValidatedQuery)
    assert result.tables <= IN_SCOPE
    assert result.functions <= ALLOWED_FUNCTIONS
    assert result.applied_limit <= ROW_CAP + 1
    again = validate_sql(result.sql, SCOPE, row_cap=ROW_CAP)
    assert isinstance(again, ValidatedQuery)
    assert (again.sql, again.tables, again.columns, again.applied_limit) == (
        result.sql, result.tables, result.columns, result.applied_limit,
    )


@settings(max_examples=10_000, deadline=None, suppress_health_check=list(HealthCheck))
@given(base=st.sampled_from(BASES), ops=tree_ops, tail=st.sampled_from(TAILS), prefix=st.sampled_from(PREFIXES))
def test_T_U6_tree_mutations_never_break_invariants(base, ops, tail, prefix):
    check_invariants(mutate_tree(base, ops, tail, prefix))


@settings(max_examples=3_000, deadline=None, suppress_health_check=list(HealthCheck))
@given(base=st.sampled_from(BASES), edits=token_edits)
def test_T_U6_token_edits_never_break_invariants(base, edits):
    check_invariants(splice_tokens(base, edits))


@settings(max_examples=2_000, deadline=None, suppress_health_check=list(HealthCheck))
@given(base=st.sampled_from(BASES), edits=char_edits)
def test_T_U6_char_splices_never_break_invariants(base, edits):
    check_invariants(splice(base, edits))


def test_T_U6_the_fuzzer_reaches_the_accept_path():
    """A fuzzer that is rejected at the parser every time proves nothing: require real acceptance."""
    import random

    rng = random.Random(7)
    accepted = 0
    for _ in range(600):
        base = rng.choice(BASES)
        edits = [(rng.choice(["insert", "replace", "delete", "duplicate"]), rng.randint(0, 10_000), rng.choice(MIXED_VOCAB))
                 for _ in range(rng.randint(1, 3))]
        accepted += isinstance(validate_sql(splice_tokens(base, edits), SCOPE, row_cap=ROW_CAP), ValidatedQuery)
    assert accepted >= 25, accepted


def test_T_U6_tree_mutations_reach_the_accept_path_too():
    import random

    rng = random.Random(11)
    accepted = rejected_by_scope = 0
    for _ in range(400):
        ops = [(rng.choice(["table", "expr", "expr"]), rng.randint(0, 10_000), rng.randint(0, 10_000)) for _ in range(rng.randint(1, 3))]
        sql = mutate_tree(rng.choice(BASES), ops, rng.choice(TAILS), rng.choice(PREFIXES))
        result = validate_sql(sql, SCOPE, row_cap=ROW_CAP)
        accepted += isinstance(result, ValidatedQuery)
        rejected_by_scope += isinstance(result, Rejection) and result.code.value in {"table_not_in_scope", "function_not_allowed"}
    assert accepted >= 20 and rejected_by_scope >= 40, (accepted, rejected_by_scope)


@settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
@given(text=st.text(max_size=300))
def test_T_U6_arbitrary_text_never_raises(text):
    result = validate_sql(text, SCOPE, row_cap=ROW_CAP)
    assert isinstance(result, (ValidatedQuery, Rejection))
