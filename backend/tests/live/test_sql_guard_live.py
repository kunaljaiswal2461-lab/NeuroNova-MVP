"""SPEC-04 live layer: T-L1 .. T-L5 against the Part 0 fixture (run inside the runner container)."""
from __future__ import annotations

import asyncio
import re
import uuid

import asyncpg
import pytest
import pytest_asyncio
import sqlglot
from hypothesis import HealthCheck, given, settings, strategies as st

from app.exceptions.custom_exceptions import NotFoundError
from app.services import catalog_service
from app.services.sql_guard.allowlist import SYNTAX_CONSTRUCTS
from app.services.sql_guard import (
    ALLOWED_FUNCTIONS,
    Rejection,
    ValidatedQuery,
    load_catalog_scope,
    validate_sql,
)
from evals import pgrun
from evals.bootstrap import IN_SCOPE_TABLES
from evals.models import load_questions
from tests.sql_guard import fuzz
from tests.sql_guard.conftest import ROW_CAP, all_accept_sqls, make_scope

# Module-scoped loop and fixtures. With pytest-asyncio 0.25 a later module's *session*-scoped fixtures and its
# tests can end up on different event loops once function-loop modules have run in between (the other live
# modules only avoid it because test_bootstrap runs first). One loop per module keeps the fixture connections,
# the engine pool and the tests together.
pytestmark = [pytest.mark.live_pg, pytest.mark.asyncio(loop_scope="module")]

ACCEPT = all_accept_sqls()
MAX_COMPARE_ROWS = 100_000


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def fixture_context():
    """This module's own source and catalog snapshot (shadows the shared session fixture on purpose)."""
    from evals.bootstrap import bootstrap

    ctx = await bootstrap()
    yield ctx
    await ctx.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_scope(fixture_context):
    ctx = fixture_context
    async with ctx.session_factory() as session:
        return await load_catalog_scope(
            session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=ctx.snapshot_id
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def grader_conn():
    conn = await pgrun.connect_grader()
    yield conn
    await conn.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def reader_conn():
    import os

    conn = await pgrun.connect_as("nn_reader", os.environ["NN_READER_PASSWORD"])
    yield conn
    await conn.close()


# ── exact execution and comparison (not the lenient eval grader) ───────────────────────────────

async def run_exact(conn, sql: str, *, search_path: str | None = None, limit: int = MAX_COMPARE_ROWS):
    async with conn.transaction(readonly=True):
        if search_path:
            await conn.execute(f"SET LOCAL search_path = {search_path}")
        stmt = await conn.prepare(sql)
        columns = [(a.name, a.type.name) for a in stmt.get_attributes()]
        cursor = await stmt.cursor()
        rows = await cursor.fetch(limit + 1)
    assert len(rows) <= limit, "comparison result too large"
    return columns, [tuple(r) for r in rows]


def _has_top_level_order(sql: str) -> bool:
    return sqlglot.parse_one(sql, read="postgres").args.get("order") is not None


EXCLUDED: list[tuple[str, str]] = []


async def assert_same_result(conn, original: str, validated: ValidatedQuery, label: str) -> None:
    ocols, orows = await run_exact(conn, original, search_path="shop, pg_catalog")
    vcols, vrows = await run_exact(conn, validated.sql)
    assert vcols == ocols, "column names or types differ"
    ordered = _has_top_level_order(original)
    if len(orows) > validated.applied_limit:
        if not ordered:
            EXCLUDED.append((label, f"{len(orows)} rows exceed the cap and the query has no ORDER BY"))
            return
        orows = orows[: validated.applied_limit]
    if not ordered:
        orows, vrows = sorted(orows, key=repr), sorted(vrows, key=repr)
    assert vrows == orows


# ── T-L1: semantic preservation ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cid,sql", ACCEPT, ids=[c for c, _ in ACCEPT])
async def test_T_L1_regenerated_sql_returns_identical_results(cid, sql, live_scope, grader_conn):
    result = validate_sql(sql, live_scope, row_cap=ROW_CAP)
    assert isinstance(result, ValidatedQuery), result
    await assert_same_result(grader_conn, sql, result, cid)


# String-literal forms and risky functions measured in SPEC-04 F8, plus the forms found while probing.
# Every one must be accepted and must return identical results.
ROUND_TRIP_EXTRAS = {
    "str-doubled-quote": "SELECT 'O''Brien' AS s",
    "str-dollar": "SELECT $$it's$$ AS s",
    "str-dollar-tagged": "SELECT $t$a''b$t$ AS s",
    "str-backslash": "SELECT 'a\\b' AS s",
    "str-trailing-backslash": "SELECT '\\' AS s",
    "str-unicode-escape": "SELECT U&'d\\0061t\\+000061' AS s",
    "str-dollar-with-sql": "SELECT $$x; DROP TABLE y; --$$ AS s",
    "str-unicode": "SELECT 'héllo ✓ 日本' AS s",
    "str-comment-markers": "SELECT '/* not a comment */ -- nor this' AS s",
    "fn-to-char-date": "SELECT to_char(ordered_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI') AS d FROM shop.orders ORDER BY id LIMIT 20",
    "fn-to-char-number": "SELECT to_char(total_amount, 'FM999,999,990.00') AS t FROM shop.orders ORDER BY id LIMIT 20",
    "fn-string-agg": "SELECT string_agg(name, ', ' ORDER BY name) AS n FROM shop.regions",
    "fn-mod-operator": "SELECT id, id % 7 AS m, mod(id, 5) AS n FROM shop.orders ORDER BY id LIMIT 20",
    "fn-date-part": "SELECT date_part('dow', ordered_at AT TIME ZONE 'Asia/Kolkata') AS d, count(*) FROM shop.orders GROUP BY 1 ORDER BY 1",
    "fn-extract-dow": "SELECT extract(dow FROM ordered_at AT TIME ZONE 'Asia/Kolkata') AS d, count(*) FROM shop.orders GROUP BY 1 ORDER BY 1",
    "fn-extract-epoch": "SELECT extract(epoch FROM ordered_at) AS e FROM shop.orders ORDER BY id LIMIT 10",
    "fn-age": "SELECT age(timestamp '2026-06-01', timestamp '2025-01-15') AS a",
    "fn-distinct-on": "SELECT DISTINCT ON (customer_id) customer_id, id FROM shop.orders WHERE customer_id IS NOT NULL ORDER BY customer_id, id LIMIT 50",
    "fn-regex-ci": "SELECT count(*) FROM shop.customers WHERE full_name ~* '^ra'",
    "fn-similar-to": "SELECT count(*) FROM shop.customers WHERE city SIMILAR TO '(Mum|Del)%'",
    "fn-position": "SELECT position('a' IN name) AS p FROM shop.regions ORDER BY id",
    "fn-round-trunc": "SELECT round(total_amount, 1) AS r, trunc(total_amount) AS t FROM shop.orders ORDER BY id LIMIT 20",
    "fn-percentile-disc": "SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY total_amount) AS p FROM shop.orders",
    "fn-mode": "SELECT mode() WITHIN GROUP (ORDER BY status) AS m FROM shop.orders",
    "fn-date-spine": "SELECT d::date AS d FROM generate_series(DATE '2026-01-01', DATE '2026-04-01', INTERVAL '1 month') AS g(d)",
    "fn-interval-arithmetic": "SELECT ordered_at + INTERVAL '3 days' AS later FROM shop.orders ORDER BY id LIMIT 10",
    "fn-rollup": "SELECT channel, status, count(*) FROM shop.orders GROUP BY ROLLUP (channel, status) ORDER BY 1, 2, 3",
    "fn-to-date": "SELECT to_date('2026-03-05', 'YYYY-MM-DD') AS d",
    "fn-substring-from-for": "SELECT substring(full_name FROM 1 FOR 3) AS s FROM shop.customers ORDER BY id LIMIT 10",
    "fn-trim-both": "SELECT trim(BOTH ' ' FROM segment) AS s FROM shop.customers ORDER BY id LIMIT 10",
    "fn-coalesce-nullif": "SELECT coalesce(nullif(phone, ''), 'none') AS p FROM shop.customers ORDER BY id LIMIT 10",
    "fn-left-right": "SELECT left(full_name, 2) AS l, right(full_name, 2) AS r FROM shop.customers ORDER BY id LIMIT 10",
    "fn-lpad": "SELECT lpad(id::text, 6, '0') AS p FROM shop.orders ORDER BY id LIMIT 10",
    "cast-numeric-params": "SELECT total_amount::numeric(12,1) AS n FROM shop.orders ORDER BY id LIMIT 10",
    "cast-varchar-params": "SELECT status::varchar(3) AS v FROM shop.orders ORDER BY id LIMIT 10",
    "cast-double": "SELECT total_amount::double precision AS d FROM shop.orders ORDER BY id LIMIT 10",
    "cast-real": "SELECT total_amount::real AS d FROM shop.orders ORDER BY id LIMIT 10",
    "cast-bpchar-char": "SELECT status::char(2) AS c FROM shop.orders ORDER BY id LIMIT 10",
    "cast-timestamp-without-tz": "SELECT ordered_at::timestamp AS t FROM shop.orders ORDER BY id LIMIT 10",
    "lit-date-timestamptz": "SELECT count(*) FROM shop.orders WHERE ordered_at >= TIMESTAMPTZ '2026-01-01 00:00+05:30' AND delivered_on > DATE '2026-02-01'",
    "op-null-safe": "SELECT count(*) FROM shop.orders WHERE customer_id IS NOT DISTINCT FROM NULL",
    "op-between": "SELECT count(*) FROM shop.orders WHERE total_amount BETWEEN 100 AND 500",
    "op-concat": "SELECT full_name || ' <' || email || '>' AS c FROM shop.customers ORDER BY id LIMIT 5",
    "op-case": "SELECT CASE WHEN total_amount > 1000 THEN 'big' WHEN total_amount > 100 THEN 'mid' ELSE 'small' END AS s, count(*) FROM shop.orders GROUP BY 1 ORDER BY 1",
    "win-lag-lead": "SELECT id, lag(total_amount) OVER (ORDER BY id) AS p, lead(total_amount, 2) OVER (ORDER BY id) AS n FROM shop.orders ORDER BY id LIMIT 20",
    "win-frame": "SELECT id, sum(total_amount) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS s FROM shop.orders ORDER BY id LIMIT 20",
    "win-ntile-rank": "SELECT id, ntile(4) OVER (ORDER BY id) AS q, rank() OVER (ORDER BY total_amount DESC, id) AS r FROM shop.orders ORDER BY id LIMIT 20",
    "union-all-limit": "SELECT id FROM shop.orders UNION ALL SELECT id FROM shop.refunds ORDER BY 1 LIMIT 15",
    "intersect-except": "SELECT customer_id FROM shop.orders INTERSECT SELECT customer_id FROM shop.page_views ORDER BY 1 LIMIT 15",
    "lateral-subquery": "SELECT o.id, x.n FROM shop.orders o CROSS JOIN LATERAL (SELECT count(*) AS n FROM shop.order_items i WHERE i.order_id = o.id) x ORDER BY o.id LIMIT 15",
    "quoted-identifiers": 'SELECT "Event Type", "order" FROM shop."Order Events" ORDER BY id LIMIT 10',
    "unqualified-table": "SELECT count(*) FROM orders",
    "mixed-case-table": "SELECT count(*) FROM SHOP.ORDERS",
    "fetch-first": "SELECT id FROM shop.orders ORDER BY id FETCH FIRST 5 ROWS ONLY",
    "offset-limit": "SELECT id FROM shop.orders ORDER BY id LIMIT 5 OFFSET 7",
    "limit-all": "SELECT id FROM shop.regions ORDER BY id LIMIT ALL",
    "ordinal-order": "SELECT status, count(*) FROM shop.orders GROUP BY 1 ORDER BY 2 DESC, 1",
}


# Owner decision D1 (specs/spec04.descisions.md): date_part regenerates as EXTRACT, so the result type changes
# from double precision to numeric with the same values. Asserted explicitly instead of compared exactly.
DECIDED_TYPE_CHANGES = {"fn-date-part": ("float8", "numeric")}


@pytest.mark.parametrize("name", sorted(ROUND_TRIP_EXTRAS))
async def test_T_L1_round_trip_extras(name, live_scope, grader_conn):
    sql = ROUND_TRIP_EXTRAS[name]
    result = validate_sql(sql, live_scope, row_cap=ROW_CAP)
    assert isinstance(result, ValidatedQuery), result
    if name in DECIDED_TYPE_CHANGES:
        original_type, validated_type = DECIDED_TYPE_CHANGES[name]
        ocols, orows = await run_exact(grader_conn, sql, search_path="shop, pg_catalog")
        vcols, vrows = await run_exact(grader_conn, result.sql)
        assert [c[1] for c in ocols] == [original_type, "int8"]
        assert [c[1] for c in vcols] == [validated_type, "int8"]
        assert [c[0] for c in vcols] == [c[0] for c in ocols]
        assert vrows == orows                      # Decimal(n) == float(n): equal row by row
        return
    await assert_same_result(grader_conn, sql, result, f"extra/{name}")


async def test_adjacent_string_literals_differential_is_only_a_concatenation(live_scope, grader_conn):
    """SPEC-04 §15: Postgres rejects `'a' 'b'` on one line; sqlglot makes it CONCAT. Effect: concatenation only."""
    result = validate_sql("SELECT 'a' 'b' AS s", live_scope, row_cap=ROW_CAP)
    assert isinstance(result, ValidatedQuery)
    with pytest.raises(asyncpg.PostgresSyntaxError):
        await run_exact(grader_conn, "SELECT 'a' 'b' AS s")
    _, rows = await run_exact(grader_conn, result.sql)
    assert rows == [("ab",)]


def test_T_L1_exclusions_are_reported():
    """Runs last in this module: print every excluded comparison and its reason (SPEC-04 §13.2)."""
    print(f"\nT-L1 excluded comparisons: {len(EXCLUDED)}")
    for label, reason in EXCLUDED:
        print(f"  {label}: {reason}")


# ── T-L2: leak oracle ───────────────────────────────────────────────────────────────────────────

def _collect_accepted(scope, examples: int) -> list[str]:
    accepted: list[str] = []

    @settings(max_examples=examples, deadline=None, database=None, suppress_health_check=list(HealthCheck))
    @given(base=st.sampled_from(fuzz.BASES), ops=fuzz.tree_ops,
           tail=st.sampled_from(fuzz.TAILS), prefix=st.sampled_from(fuzz.PREFIXES))
    def probe(base, ops, tail, prefix):
        result = validate_sql(fuzz.mutate_tree(base, ops, tail, prefix), scope, row_cap=ROW_CAP)
        if isinstance(result, ValidatedQuery):
            accepted.append(result.sql)

    probe()
    return list(dict.fromkeys(accepted))


async def test_T_L2_no_accepted_query_leaves_the_selected_scope(live_scope, reader_conn):
    accepted = await asyncio.get_running_loop().run_in_executor(None, _collect_accepted, live_scope, 2_000)
    assert len(accepted) >= 50, "the fuzzer must reach the accept path"
    leaks: list[str] = []
    syntax_errors: list[str] = []
    errors: dict[str, int] = {}
    for sql in accepted:
        try:
            async with reader_conn.transaction(readonly=True):
                await reader_conn.execute("SET LOCAL statement_timeout = '2s'")
                await reader_conn.fetch(sql)
        except asyncpg.InsufficientPrivilegeError as exc:
            leaks.append(f"{type(exc).__name__}: {exc}")
        except asyncpg.UndefinedTableError as exc:
            relation = re.search(r'relation "([^"]+)"', str(exc))
            if relation is None or not relation.group(1).startswith("shop."):
                leaks.append(f"{type(exc).__name__}: {exc}")
        except asyncpg.PostgresError as exc:
            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
            if isinstance(exc, asyncpg.PostgresSyntaxError):
                syntax_errors.append(f"{exc} <= {sql[:240]}")
    print(f"\nT-L2 accepted={len(accepted)} other_errors={errors}")
    for sample in syntax_errors[:6]:
        print("  syntax:", sample)
    assert not leaks, leaks[:5]


# ── T-L3: allowlist sweep ───────────────────────────────────────────────────────────────────────

async def _sweep(grader_conn):
    rows = await grader_conn.fetch(
        "SELECT p.proname, array_agg(DISTINCT p.provolatile::text) AS vol "
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'pg_catalog' AND p.proname = ANY($1::text[]) GROUP BY p.proname",
        sorted(ALLOWED_FUNCTIONS - SYNTAX_CONSTRUCTS),
    )
    found = {r["proname"]: set(r["vol"]) for r in rows}
    return sorted(ALLOWED_FUNCTIONS - SYNTAX_CONSTRUCTS - found.keys()), sorted(n for n, v in found.items() if not v <= {"i", "s"})


# Owner decision D2: six allowlisted names are SQL syntax constructs with no pg_proc row.
async def test_T_L3_every_allowed_function_exists_and_is_not_volatile(grader_conn):
    assert SYNTAX_CONSTRUCTS == {"cast", "coalesce", "greatest", "least", "nullif", "trim"}
    assert SYNTAX_CONSTRUCTS <= ALLOWED_FUNCTIONS
    missing, volatile = await _sweep(grader_conn)
    assert not volatile, volatile
    assert not missing, missing


# ── T-L4: scope builder ─────────────────────────────────────────────────────────────────────────

async def test_T_L4_scope_is_exactly_the_eight_selected_tables(live_scope):
    assert {(t.schema, t.name) for t in live_scope.tables} == {("shop", t) for t in IN_SCOPE_TABLES}
    static = {t.name: [(c.name, c.native_type) for c in t.columns] for t in make_scope().tables}
    live = {t.name: [(c.name, c.native_type) for c in t.columns] for t in live_scope.tables}
    assert live == static, "tests/sql_guard/conftest.py has drifted from the fixture catalog"


async def test_T_L4_non_owner_is_refused_like_the_compiler(fixture_context):
    ctx = fixture_context
    stranger = uuid.uuid4()
    async with ctx.session_factory() as session:
        with pytest.raises(NotFoundError) as ours:
            await load_catalog_scope(session, owner_user_id=stranger, source_id=ctx.source_id, snapshot_id=ctx.snapshot_id)
    async with ctx.session_factory() as session:
        with pytest.raises(NotFoundError) as theirs:
            await catalog_service.resolve_postgres_query_catalog_context_for_owner(
                session, source_id=ctx.source_id, snapshot_id=ctx.snapshot_id, asset_id=uuid.uuid4(),
                column_ids=(), owner_user_id=stranger,
            )
    assert str(ours.value) == str(theirs.value)


async def test_T_L4_unknown_snapshot_is_refused_like_the_compiler(fixture_context):
    ctx = fixture_context
    ghost = uuid.uuid4()
    async with ctx.session_factory() as session:
        with pytest.raises(NotFoundError) as ours:
            await load_catalog_scope(session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=ghost)
    assert "snapshot" in str(ours.value)


async def test_T_L4_historical_snapshot_is_refused_like_the_compiler():
    """A second sync publishes a new head; the first snapshot is then a stale planning context.

    Uses its own source so the shared fixture's head stays put for the other tests."""
    from app.connectors.postgres_adapter import PostgresOnboardingAdapter
    from app.db.models.catalog import CatalogSyncRun, CatalogSyncRunStatus
    from app.workers.catalog_discovery_worker import CatalogDiscoveryWorker
    from evals.bootstrap import bootstrap, test_policy

    ctx = await bootstrap()
    try:
        async with ctx.session_factory() as session:
            run = await catalog_service.request_catalog_sync_for_owner(
                session, source_id=ctx.source_id, owner_user_id=ctx.user_id
            )
            run_id = run.id
        worker = CatalogDiscoveryWorker(
            settings=ctx.settings,
            session_factory=ctx.session_factory,
            adapter_factory=lambda s: PostgresOnboardingAdapter(settings=s, policy=test_policy()),
            worker_id="sql-guard-t-l4",
        )
        for _ in range(120):
            await worker.run_once()
            async with ctx.session_factory() as session:
                status = (await session.get(CatalogSyncRun, run_id)).status
            if status is CatalogSyncRunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.25)
        assert status is CatalogSyncRunStatus.SUCCEEDED
        async with ctx.session_factory() as session:
            new_head = (
                await catalog_service.get_current_catalog_snapshot_for_owner(
                    session, source_id=ctx.source_id, owner_user_id=ctx.user_id
                )
            ).id
        assert new_head != ctx.snapshot_id

        async with ctx.session_factory() as session:
            with pytest.raises(catalog_service.CatalogQueryContextError) as ours:
                await load_catalog_scope(
                    session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=ctx.snapshot_id
                )
        async with ctx.session_factory() as session:
            with pytest.raises(catalog_service.CatalogQueryContextError) as theirs:
                await catalog_service.resolve_postgres_query_catalog_context_for_owner(
                    session, source_id=ctx.source_id, snapshot_id=ctx.snapshot_id, asset_id=uuid.uuid4(),
                    column_ids=(), owner_user_id=ctx.user_id,
                )
        assert str(ours.value) == str(theirs.value)
        async with ctx.session_factory() as session:        # the new head works
            scope = await load_catalog_scope(
                session, owner_user_id=ctx.user_id, source_id=ctx.source_id, snapshot_id=new_head
            )
        assert len(scope.tables) == len(IN_SCOPE_TABLES)
    finally:
        await ctx.dispose()


# ── T-L5: end to end through the real driver and role ────────────────────────────────────────────

FAMILIES = {"aggregate": "agg-001", "join_2": "join2-001", "time_bucket": "time-001",
            "window": "win-001", "awkward_identifiers": "ident-001"}


@pytest.mark.parametrize("family", sorted(FAMILIES))
async def test_T_L5_validated_sql_runs_as_the_restricted_reader(family, live_scope, reader_conn, grader_conn):
    question = next(q for q in load_questions("golden") if q.id == FAMILIES[family])
    reference = question.expect.reference_sql
    result = validate_sql(reference, live_scope, row_cap=ROW_CAP)
    assert isinstance(result, ValidatedQuery), result
    _, expected = await run_exact(grader_conn, reference)
    _, actual = await run_exact(reader_conn, result.sql)
    ordered = _has_top_level_order(reference)
    assert (actual if ordered else sorted(actual, key=repr)) == (expected if ordered else sorted(expected, key=repr))
