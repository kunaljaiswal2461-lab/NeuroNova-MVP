# Stage 9B — NL→SQL validator (SPEC-04, Part 1)

Code: `backend/app/services/sql_guard/`. Tests: `backend/tests/sql_guard/` (no database) and
`backend/tests/live/test_sql_guard_live.py` (the Part 0 fixture).

## The boundary

From Part 2 onwards a model writes SQL that runs against a customer's database. Four layers stand between
that SQL and harm. **The database role is the real security boundary.** The validator is defence in depth
and a quality tool: it turns a round trip ending in Postgres error `42703` into a message the repair loop
can act on before any connection opens. No later part may relax layers 1 and 2 because this layer exists.

| Layer | Where | Stops |
|---|---|---|
| 1. Database role | Stage 2 / 4B | Whatever the restricted reader role cannot do: writes, DDL, other schemas |
| 2. Session policy | Stage 4B | Writes (read-only transaction), slow queries (timeouts), function hijacking (`search_path = pg_catalog`), huge results (row and byte caps) |
| 3. **SQL validator** | **this stage** | Side-effect and information-leaking functions that read-only mode allows (`pg_sleep`, advisory locks, `version()`, `current_user`, system catalogs), tables outside the selected scope, unbounded results, malformed SQL |
| 4. Cost guard | Part 2 | Valid but expensive queries |

## Contract

```python
result = validate_sql(sql, scope, row_cap=500)   # ValidatedQuery | Rejection, never raises
```

* **Execute `result.sql`, never the model's text.** The validator parses the model's SQL, checks the tree,
  and regenerates SQL from that tree. Two parsers (sqlglot and Postgres) can disagree about the same text;
  executing the regenerated text means Postgres runs what was checked. Example measured on sqlglot 30.21.0:
  `SELECT U&"\0070g_sleep"(1)` is `pg_sleep(1)` to Postgres and `U & "\0070G_SLEEP"(1)` to sqlglot.
* The regenerated text is validated again and must be a fixed point; any difference is `internal_error`.
* Comments are dropped from the regenerated text.
* Exactly two rewrites: unqualified catalog tables become schema-qualified (the executor session uses
  `search_path = pg_catalog`, so an unqualified name would resolve to a system catalog), and the top-level
  `LIMIT` becomes `model_limit` if it is within `row_cap`, else `row_cap + 1` (the extra row lets Part 2
  tell "exactly the cap" from "truncated").
* Messages (`Rejection.message`) hold only fixed wording, catalog names and the offending identifier cut to
  64 characters. No SQL, no `;` or `$`, no database error text.

## What is allowlisted

Everything is an allowlist (`allowlist.py`); whatever has not been reviewed is rejected.

1. **sqlglot node classes.** `version()`, `current_user` and `generate_series` in FROM are their own classes,
   not generic function calls, so a name list alone would miss them.
2. **Function names as Postgres reads them**, from the token stream (a name followed by `(`). Quoted names
   are never allowed. Names the query declares itself (CTEs, aliases) are exempt.
3. **Cast target types.** `'pg_authid'::regclass` is harmless-looking syntax that resolves system objects.

Tables are resolved by the validator itself (`_check_tables`), not by sqlglot's scope analysis; see the
learning file for why.

## Known regeneration differences

* `date_part` → `EXTRACT`: result type `double precision` → `numeric`, values unchanged (owner decision D1).
* `current_timestamp(3)` loses its precision argument; the only effect is fractional-second rounding.
* `'a' 'b'` on one line becomes `CONCAT('a','b')` (Postgres would reject the original).

## Owner decisions (specs/spec04.descisions.md)

* D1: accept `date_part` → `EXTRACT` type change; `date_part` stays allowed.
* D2: `cast`, `coalesce`, `greatest`, `least`, `nullif`, `trim` are syntax constructs, exempt from the pg_proc sweep.
* D3: `E''`, `N''`, `B''`, `X''` strings stay rejected.
* D4: the 30-table limit counts every catalog table reference node.
* D5: unquoted SQL special-value keywords as columns, and the `ROWS FROM` token rule, are part of the contract.
* D6: rejection codes follow stage order as implemented in `reject_cases.yaml`; Part 6 builds on them.

## What it does not protect against

* **A malicious or compromised database role.** If the reader role can do something, the validator is not
  what stops it.
* **Expensive queries.** A cartesian join, a full scan of a large table, `generate_series(1, 10^12)` are
  all valid. Part 2's EXPLAIN guard owns cost; the statement timeout is the backstop.
* **Wrong answers.** A valid query can answer a different question. That is what the Part 0 eval measures.
* **Wall-clock dependence.** `now()` and `CURRENT_DATE` are allowed, so results depend on when they run.
* **`standard_conforming_strings = off`.** The executor session must keep Postgres's default (`on`);
  backslashes in string literals are regenerated as written.
* **sqlglot upgrades.** Node classes and regeneration change between versions. sqlglot is pinned at
  30.21.0; an upgrade is a deliberate change that re-runs T-U1..T-U6, T-L1 and T-L3.
* **`'a' 'b'` on one line.** Postgres rejects it; sqlglot turns it into `CONCAT('a','b')`. The only possible
  effect is a literal concatenation.
* Views, partitioned tables and multiple sources are not supported (unchanged from Stage 4B).

## Settings

`sql_guard_max_chars` (20,000), `sql_guard_max_paren_depth` (32), `sql_guard_max_ast_nodes` (3,000), plus a
constant limit of 30 catalog table references. The corpus maximum is 432 characters, depth 4, 95 nodes,
4 tables.

## Added in Part 2: `no_table_reference`

A query must read at least one catalog table. `SELECT 1`, a bare `generate_series(...)`, or a CTE over
literals is rejected with `no_table_reference` ("The query must read from at least one available table.").
The check runs after the table, column, function and cast stages, so those keep their more specific codes.

## Added in Part 2: `lpad` / `rpad` length limit

The length argument of `lpad` and `rpad` must be a whole-number literal of at most 10,000. Anything else (a column, an
expression, a cast, a subquery, a negative or decimal number) is `function_not_allowed` with "The length argument of lpad and
rpad must be a whole-number literal of at most 10,000.". This only stops the cheapest way to build a huge value; the
per-row size guard in Part 2 (`docs/stage9c-sql-execution.md`) is the real backstop.
