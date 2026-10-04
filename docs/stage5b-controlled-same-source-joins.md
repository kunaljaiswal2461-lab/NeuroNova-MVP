# Stage 5B controlled same-source joins

Stage 5B adds one deliberately narrow join capability to the controlled
PostgreSQL chat path. It is not a general multi-table SQL feature and it does
not accept SQL, an `ON` expression, table names, column mappings, or formulas
from a browser or model.

## Supported boundary

The only executable joined shape is:

```text
confirmed metric on base table
  → one owner-confirmed catalog FK relationship
  → uniquely keyed lookup table in the same source/current snapshot
```

- Exactly two ordinary PostgreSQL tables from one source and one current
  selected catalog snapshot.
- A declared FK from the designated base table to an exact ordered declared
  primary or unique key on the lookup table. Composite key mappings are
  equality pairs in catalog ordinal order.
- Owner-selected `INNER` or `LEFT` behavior, retained as an immutable
  relationship version.
- A confirmed metric aggregate remains on its base table. Lookup columns may
  supply grouping or extra narrowing filters only.
- For `LEFT` joins, a lookup predicate must explicitly say whether it applies
  in `ON` (`join_on`, retain unmatched base rows) or `WHERE` (`where`, exclude
  unmatched base rows). Base filters remain ordinary `WHERE` predicates.

SQL equality does not match `NULL`: if any foreign-key or target-key component
is null, that base row has no lookup match. An `INNER` join excludes it; a
`LEFT` join retains it unless a lookup-side `WHERE` predicate removes it.
Composite keys compare every ordered FK/key pair together—there is no partial
component match.

`count_all` counts joined base rows under the approved relationship/filter
semantics; `count_column` excludes null values in its selected column. Stage
5B does not use `DISTINCT` as a repair for duplicate aggregates: the lookup
target's exact PK/UNIQUE evidence is required before compilation and again at
the live gateway.

## Explicitly deferred

Cross-source, multi-hop, many-to-many, row-multiplying, right/full/cross, and
manual/inferred joins are unsupported. So are arbitrary `ON` expressions,
raw SQL, lookup-table metric aggregation, multiple-table formulas, profiling,
charts, and new connectors.

## Relationship lifecycle and APIs

Migration `0013_confirmed_relationship_definitions` adds immutable
`relationship_definition_versions` and a versioned logical definition.
Confirming a relationship accepts only the current catalog snapshot ID, an
in-scope declared foreign-key ID, explicit owner confirmation, and `inner` or
`left`. The backend derives every table, ordered key pair, and target-key fact
from the catalog.

JWT-only source-owner endpoints are:

- `GET/POST /sources/{source_id}/relationships`
- `GET /sources/{source_id}/relationships/eligible`
- `GET /sources/{source_id}/relationships/{relationship_id}`
- `GET/POST /sources/{source_id}/relationships/{relationship_id}/versions`
- `POST /sources/{source_id}/relationships/{relationship_id}/retire`

Catalog refresh, scope removal, changed mapping, missing assets, or changed
target-key evidence makes an active definition `needs_review`; retirement
blocks new executions while preserving history. IDs remain source-scoped and
all version references are independently checked for source ownership.

## Execution and provenance

The typed PostgreSQL plan carries an ID-only approved relationship pin.
Compiler and executor re-resolve it against the current owner-authorized
catalog before SQL is built or sent remotely. The remote gateway validates both
relations, both column sets, role/privilege/RLS restrictions, target key
uniqueness/deferrability/validity, and takes relation locks in a stable order.
After both relation locks are held, it also revalidates the exact declared FK
constraint name and its ordered base-to-lookup column mapping before preparing
or executing the statement.

`postgres_query_runs` stores an optional
`relationship_definition_version_id` with both a simple and a source-local
composite foreign key. Results contain only safe relationship provenance (ID,
version, and join type), never SQL, fixed filter values, credentials, or rows
in ordinary audit data. Metric and relationship pins are checked before run
creation, immediately before remote work, after remote work, and before chat
result delivery.

## Verification status

Unit, SQLite catalog-state, compiler, fake asyncpg-adapter, API, planner,
chat, and frontend component tests cover the controlled contracts. They do not
prove actual PostgreSQL behavior. The live disposable PostgreSQL procedure in
`docs/stage2d-postgresql-integration.md` remains required for known-answer
two-table results, nullable keys, composite keys, target-key DDL changes,
deferrable constraints, two-relation lock/cancellation behavior, TLS/ACL/RLS,
and rendered browser verification.
