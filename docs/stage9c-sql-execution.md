# Stage 9C — Executing validated SQL (SPEC-05, Part 2)

Code: `backend/app/services/sql_execution.py` (service), `backend/app/connectors/postgres_query_executor.py`
(`PostgresQueryExecutionAdapter.execute_sql`), `backend/app/services/sql_guard/seal.py` (the sealed query).
Tests: `tests/sql_guard/test_seal.py`, `tests/catalog/test_sql_execution.py` (no database),
`tests/live/test_sql_execution_live.py` (the Part 0 fixture).

## Contract

```python
result = await execute_validated_sql(
    session_factory=..., settings=..., user_id=..., source_id=..., sql=model_sql,
)                                   # SqlExecutionResult | Rejection
```

* A `Rejection` is the validator's answer, returned as is (Part 6 turns it into a repair prompt).
* A `SqlExecutionResult` has `status` `succeeded | failed | cancelled`. A failure carries a `failure_code` and
  a fixed `message`, never database or driver text. `sql` is always the validator's regenerated text.
* Nothing is retried. A failed or cancelled run is final.

## Order of work (service)

1. Authorize: the caller owns the source, it is active, and a catalog head exists.
2. Validate against the **current head** (a query that reads no catalog table is rejected: `no_table_reference`) (`load_catalog_scope` + `validate_sql`). The caller's string is
   never executed, and SQL validated against an older snapshot is validated again.
3. Seal: `sql_guard.seal()` re-runs the validator on the text and refuses unless it regenerates identically.
4. Take a limiter slot (global / per user / per source).
5. Create the run row (`query_kind = 'sql'`, structural fingerprint, asset ids; **no SQL text, no literal**).
6. Re-check the head is still the validated snapshot, load credentials, call the adapter.
7. Serialize rows as they stream (row and byte caps).
8. Deliver under `postgres_query_delivery_guard` (source row lock, head and scope unchanged since the read).
9. Finalize the run.

## Order of checks (adapter, `execute_sql`), one read-only transaction

| # | Check | Failure code |
|---|---|---|
| 0 | The object is a minted `SealedValidatedQuery`; the facts describe exactly its tables (1 to 30) | `unsupported_execution_configuration` (before any connection) |
| 1 | Session policy (shared with the compiled path): read-only, `search_path = pg_catalog`, UTC, timeouts; `client_connection_check_interval = 250ms` so a cancelled query stops on the server too (PostgreSQL 14+) | `unsupported_execution_configuration` |
| 2 | Identity check of each relation, then `LOCK TABLE … IN ACCESS SHARE MODE` in canonical `(schema, table)` order | `schema_changed`, `access_changed`, `restricted_role_required` |
| 3 | Re-check each relation after the lock (same OID) and the referenced columns (name, type, nullability, privilege) | `schema_changed`, `access_changed` |
| 4 | `PREPARE` the sealed text inside the **row-size guard** (below); read the output attributes | |
| 5 | **Type gate**: every output type is one the serializer represents exactly | `unsupported_output_type` |
| 6 | **Cost gate**: `EXPLAIN (FORMAT JSON)` of the wrapped text, total cost `<= postgres_sql_max_plan_cost` | `query_too_expensive` |
| 7 | Open the cursor; read each row **by position**; the server refuses a row over `postgres_sql_max_row_bytes` | `result_row_too_large`, `execution_timeout` (5 s backstop), `execution_cancelled` |

### Row-size guard

The cost gate cannot see a cheap plan that builds a huge value (`replace(lpad(…10000), 'x', lpad(…10000))` is 100 MB,
`CAST('a' AS CHAR(10000000))` is 10 MB). So the sealed text is wrapped:

```sql
SELECT q.* FROM (<sealed sql>) AS q
WHERE CASE WHEN octet_length(ROW(q.*)::text) > <limit> THEN CAST('nn_row_too_large:' || octet_length(ROW(q.*)::text) AS int) = 0 ELSE true END
```

`ROW(q.*)` names the whole row on purpose: a bare `q::text` resolves to a *column* named `q` when the query outputs one, which would skip the guard.
The server fails the statement (SQLSTATE 22P02, prefix `nn_row_too_large:`) before the row is sent. The failing branch
depends on the row, otherwise the planner folds it and every query fails. The limit is an application integer, default
1 MiB. The `lpad`/`rpad` length cap (an integer literal of at most 10,000) stays in the validator as the cheap first line.

Relations must be ordinary tables (`relkind = 'r'`). Views, materialized views, partitioned tables,
partitions, inheritance parents and children, and RLS-enabled tables are refused: they have extra execution
semantics that this part does not model. The role must not be a superuser, bypass RLS or own the table.

No gate opens a cursor. A refused query has cost one connection, a few catalog reads and one `EXPLAIN`.

## Output rules

* Labels keep Postgres's names in order. Duplicates get `_2`, `_3` (`count`, `count_2`). An empty name or
  `?column?` becomes `col_<n>` (1-based). Values are read by position because asyncpg's by-name lookup
  returns the **last** duplicate: `SELECT count(*), count(*) FILTER (…)` reads `[42020, 42020]` by name and
  `[60000, 42020]` by position.
* Supported output types: `int2 int4 int8 numeric float4 float8 text varchar bpchar bool uuid date timestamp
  timestamptz`. `interval` (asyncpg's `timedelta` is lossy for months), composite and `record` values,
  `money` and arrays are refused with a one-line repair hint naming the column's position.
* `date`, `timestamp` and `timestamptz` are read as PostgreSQL's ISO text (text codecs; the connection pins
  `DateStyle = 'ISO, YMD'`) on both the compiled and the SQL path. `infinity`/`-infinity` are returned as those words and a real
  `9999-12-31` stays `9999-12-31`. Numeric and float `NaN`/`±Infinity` are `non_finite_number` strings. Years above 9999 and BC
  values are returned exactly as PostgreSQL writes them (e.g. `0044-03-15 BC`, `12345-01-02 03:04:05+00`).
* `safety_row_truncated` is set only when the validator applied `row_cap + 1` (no LIMIT, or one above the
  cap) and more than `row_cap` rows arrived. A model LIMIT within the cap is the answer's own meaning.

## Why a cost ceiling, and why 5,000,000

`EXPLAIN` cost is the planner's estimate in arbitrary units. It is not time, but it separates two populations
by orders of magnitude on the fixture:

| | cost |
|---|---|
| corpus (482 queries) p50 / p95 / max | 1,664 / 9,814 / 511,039 |
| `page_views × orders` cross join | 1.8e9 |
| `page_views` self join on `path` | 9.9e9 |
| `orders × order_items` cross join | 1.26e8 |
| `generate_series(1, 1e9)` | 1.25e7 |

The ceiling sits one decade above the most expensive legitimate query and below the cheapest pathological
one. Rejection takes under a second, against 5 s of database time for a statement-timeout death.

Cost is also **not** time: a function's `ROWS` estimate or stale statistics can make a slow query look cheap
(or the reverse), so the 5 s statement timeout stays as the backstop. The estimate only means something with
planner statistics, which is why the fixture runs `ANALYZE` last. Against a customer database the same holds:
a table that has never been analyzed gets the planner's defaults. `EXPLAIN ANALYZE` is never used; it would
execute the query.

## Error codes

| Code | Meaning |
|---|---|
| `unsupported_output_type` | A result column has a type the serializer cannot represent (message names the column position and a hint) |
| `query_too_expensive` | Plan cost above the ceiling |
| `result_row_too_large` | One result row's text form exceeded `postgres_sql_max_row_bytes`; refused by the server, nothing was sent |
| `schema_changed` | A table, column, type or relation kind differs from the catalog snapshot |
| `access_changed` | A privilege was revoked |
| `restricted_role_required` | The role is a superuser, bypasses RLS, owns the table, or the table has RLS |
| `access_revoked` | The source or head changed while the query ran; rows are withheld |
| `execution_timeout` | The 5 s statement timeout (or a lock timeout) |
| `execution_cancelled` | Cancelled by the caller; the connection is terminated |
| `query_not_authorized`, `source_unavailable`, `catalog_unavailable`, … | As in Stage 4B |

## What is recorded

`postgres_query_runs` gets `query_kind` (`compiled` or `sql`); a `sql` run has `asset_id IS NULL` and one row
per table read in `postgres_query_run_assets`. `plan_fingerprint` holds the **structural fingerprint**: the
validated SQL with every literal replaced by a placeholder, hashed (SHA-256). Two queries that differ only
in literals hash identically, so a fingerprint cannot be reversed into a value. The SQL text lives only in
the chat message (Part 7).
