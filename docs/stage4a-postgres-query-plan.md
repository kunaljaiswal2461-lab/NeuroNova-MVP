# Stage 4A PostgreSQL QueryPlan and Compiler

Stage 4A establishes a deterministic planning boundary for a narrow class of
single-table PostgreSQL questions. It converts an already-authorized,
structured PostgreSQL plan into one parameterized read-statement template and
an expected-result description.

```text
trusted actor + current authorized catalog context
  -> structured PostgreSQL QueryPlan
  -> deterministic validation
  -> parameterized PostgreSQL statement + expected output contract
```

It does **not** execute a client-database query. It adds no HTTP route, LLM
planner, database-chat flow, frontend change, connector type, query-run
persistence, or migration. File upload, file-backed `QueryPlan`, Polars
compilation, and file-query execution remain separate and unchanged.

## Authorization and catalog-version policy

The plan is not an authorization capability. The authenticated actor and
permitted source context must be supplied by trusted application code from a
valid JWT-derived user identity. They must not be read from a model-produced
plan, request-supplied user ID, legacy global API key, or an absent (`None`)
identity.

Stage 4A uses a deliberately narrow **current-head-only** policy:

1. The actor must own the PostgreSQL source.
2. The trusted source/snapshot context and the plan's asset/column references
   must agree with one another in the application catalog.
3. The referenced snapshot must be the source's current published catalog
   head, associated with a successful synchronization run.
4. The snapshot's captured scope fingerprint must exactly match the source's
   current explicit table selection.
5. The selected asset must still be in that current table selection, and every
   referenced column must belong to that exact source, snapshot, and asset.

Historical snapshots remain available for the Stage 3C inspection experience,
but they are not eligible for a query plan. A plan bound to an old snapshot or
a changed scope fails; it is never silently retargeted to a newer snapshot.
This is a catalog-version policy, not proof that the live client database is
still reachable, has the same schema, or grants the same privileges.

Catalog comments and database-supplied descriptive text are untrusted content.
They may be displayed as metadata where authorized, but cannot authorize a
source, select a table, enable an operation, supply SQL, or define a business
metric.

## Supported plan surface

One plan addresses exactly one catalog asset (one PostgreSQL table). It uses
catalog asset and column identifiers rather than schema/table/column names.
The compiler resolves identifiers only from the authorized catalog records.

Supported operations are intentionally limited to:

- Explicit direct column projections.
- Typed row filters.
- `COUNT(*)`, `COUNT(column)`, `SUM`, `AVG`, `MIN`, and `MAX`.
- Grouping by supported direct columns or supported calendar/time buckets.
- Explicit sorting over a valid selected, grouping, or aggregate output.
- A required bounded result limit.

Plans have a small, strict schema. Unknown fields are rejected rather than
ignored. Output references are validated structural references; the compiler
owns SQL aliases, ordering tokens, functions, operators, and casts. A valid
plan has one of these result shapes:

- Direct projections, with optional row filtering, sorting, and limiting.
- Aggregates only, with optional row filtering and sorting.
- Grouping outputs plus aggregates, with optional row filtering, sorting, and
  limiting.

An ungrouped raw projection mixed with an aggregate is invalid. Grouping
without an aggregate, post-aggregation filters, duplicate output references,
unknown sort outputs, and unsupported output combinations are invalid.

Configured caps bound result rows, projections, filters, aggregates, grouping
dimensions, `IN` members, and a conservative total-size estimate for all bound
parameters. Text values reject NUL bytes and are bounded in UTF-8 bytes;
decimal values are finite and bounded in precision/exponent. These plan-input
caps do not replace Stage 4B's independent returned-result byte limit.

An absent explicit sort means a bounded result is unordered. It must not be
described as a top-N result. A top-N result requires an explicit sort; ties
remain unspecified unless the plan supplies sufficient explicit sort keys.

## Type and operator matrix

Support is determined from the native catalog type identity: a recognized
`pg_catalog` type name. The displayed formatted type and type OID are useful
metadata but are not the authority for an operation. Domains, custom-schema
types, arrays, and unrecognized native types fail closed.

| Native type family | Permitted row filters | Grouping | Aggregates |
| --- | --- | --- | --- |
| `int2`, `int4`, `int8`, `numeric`, `float4`, `float8` | equality, inequality, finite `IN`, ordered comparisons, null tests | direct column | `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` |
| `text`, `varchar`, `bpchar` | equality, inequality, finite `IN`, literal case-sensitive contains/not-contains, null tests | direct column | `COUNT` |
| `bool` | equality, inequality, finite `IN`, null tests | direct column | `COUNT` |
| `uuid` | equality, inequality, finite `IN`, null tests | direct column | `COUNT` |
| `date` | equality, inequality, finite `IN`, ordered comparisons, null tests | direct column or day/week/month/quarter/year bucket | `COUNT`, `MIN`, `MAX` |
| `timestamp` without time zone | equality, inequality, finite `IN`, ordered comparisons, null tests | direct column or supported bucket | `COUNT`, `MIN`, `MAX` |
| `timestamptz` | equality, inequality, finite `IN`, ordered comparisons, null tests | direct column or supported bucket with an explicit IANA time zone | `COUNT`, `MIN`, `MAX` |

`COUNT(*)` and `COUNT(column)` are distinct operations. The former counts
rows after row filters; the latter counts non-null values of the selected
column after row filters. Ordinary equality and inequality never accept a
null value: plans must use `IS NULL` or `IS NOT NULL` instead. `IN` lists must
be nonempty, bounded, and contain no null member.

Numeric inputs are strictly typed and finite. Boolean values are not accepted
as integers. Dates use an explicit date-only representation. A timestamp
without time zone uses a naive datetime and is never implicitly converted.
A timestamp with time zone requires an offset-aware instant; bucket operations
also require an explicit validated IANA time zone. The compiler never turns
phrases such as "this month" into time boundaries and never infers a business
time zone.

Date bucket semantics are calendar semantics. A `timestamptz` bucket is
defined as a local timestamp in the declared IANA time zone, not as an output
depending on the connection's ambient `TimeZone` setting. Date-only ranges and
timestamp ranges are distinct typed operations: the initial contract expresses
an explicit half-open range as separate typed `gte` and `lt` row filters, not
as an implicit relative-time or `BETWEEN` expression.

The first contract excludes joins, subqueries, raw SQL, arbitrary expressions,
arbitrary function or operator names, writes, DDL, procedures, `HAVING`,
window functions, business metrics, semantic definitions, arrays, ranges,
JSON, bytea, XML, network types, time/interval types, money, full-text types,
custom types, and extension-specific operations.

## Deterministic compiler contract

Only a plan that has passed authorization and catalog validation is compiled.
Compilation produces exactly one supported `SELECT` statement and a typed
compiled-query object. That object contains the SQL template, bound parameter
sequence, source/snapshot/asset references, effective row limit, expected
output columns/types, ordering, and calculation/time semantics.

The compiler:

- Resolves schema, table, and column identifiers from authorized catalog rows
  only and quotes each identifier part independently.
- Schema-qualifies the relation and uses application-controlled table aliases.
- Builds SQL from fixed templates and allowlists for clauses, functions,
  operators, sort directions, aggregate kinds, and bucket units.
- Uses asyncpg-compatible positional parameters for values, including the
  effective row limit. It never interpolates a value into SQL.
- Applies the configured hard plan bounds even if an internal caller attempts
  to bypass ordinary request validation.
- Produces compiler-owned output aliases and an explicit result map rather
  than treating a plan field as executable SQL syntax.
- Avoids uncontrolled function resolution by qualifying built-in functions as
  appropriate and requiring the execution-session policy described below.

Bound values are sensitive operational data. They must be excluded from
ordinary logs, exception messages, string representations, telemetry, and
safe query fingerprints. A provenance/fingerprint value may include stable
source/snapshot/asset identifiers, plan structure, structural IANA timezone,
row limit, and parameter *types*, but must not include filter parameter values
or hashes of low-entropy parameter values. Normal operational logging should
prefer safe identifiers and the fingerprint over the SQL template itself.

Parameterization, a `SELECT` statement, and `LIMIT` do not by themselves make
client-database access safe, cheap, or current. Grouping, sorting, row-level
security, generated columns, privilege changes, schema changes, and database
load can still affect safety, cost, and correctness.

## Stage 4B execution handoff

The Stage 4A entry point is deliberately application-internal:

```python
compiled = await validate_and_compile_postgres_query(
    session,
    owner_user_id=trusted_jwt_user_id,
    source_id=trusted_source_id,
    snapshot_id=trusted_current_snapshot_id,
    plan=PostgresQueryPlan.model_validate(untrusted_structural_payload),
    settings=settings,
)
```

`PostgresQueryPlan` contains an asset ID, column IDs, typed values, and fixed
structural choices only; it contains no actor, source, snapshot, SQL, alias,
or credential. The returned `CompiledPostgresQuery` exposes `sql`, a
`parameters` tuple excluded from ordinary logging, source/snapshot/asset IDs,
`expected_output_columns`, `effective_limit`, `ordering`, explicit
`semantics`, a structural `fingerprint`, and `safe_provenance()`.

Stage 4B must consume only this validated compiled-query object; it must not
accept raw SQL or compile an unvalidated plan. Immediately before a remote
operation, it must repeat the application-side authorization and catalog
checks:

1. Resolve the trusted actor, source ownership, exact current catalog head,
   current scope fingerprint, selected asset, and referenced columns again.
2. Obtain only the persisted, source-bound connection configuration and
   protected credential material through the existing connector activation,
   credential, TLS, and destination-egress policy.
3. Recheck that the chosen live relation and columns remain compatible with
   the plan and that the restricted client role still has the required schema,
   table, and column read privileges. This check and execution should occur in
   the same short-lived read-only transaction where practical, so a detected
   incompatibility fails safely rather than being silently adapted.

The client-database role must be restricted to the minimum required access:
`CONNECT`, selected-schema `USAGE`, and explicit selected-table/column
`SELECT`. It must not receive write, DDL, role-management, replication,
superuser-like, broad-read, or arbitrary temporary-object capabilities.
Verified TLS, the existing destination policy, and connector activation remain
mandatory; a source record or a compiled plan does not bypass them.

The execution connection/session must be short-lived or fully reset before
reuse. It must enforce all of the following at execution time:

- Read-only transaction behavior (including a read-only default) and an
  appropriate consistent transaction isolation level.
- A controlled `search_path` such as `pg_catalog`, while retaining explicit
  schema qualification for the selected relation.
- An application-controlled `application_name`.
- Connect, statement, lock, idle-in-transaction, and whole-operation timeouts.
- Cancellation and reliable connection cleanup when a request is cancelled or
  exceeds its operation budget.
- Per-user/per-source and service-wide concurrency limits.
- Both SQL row bounds and a bounded serialized-result byte budget before a
  result is handed to presentation or SSE code.
- Safe database error classification that does not expose credentials,
  parameter values, SQL text, host details, role details, or raw driver errors.

Stage 4B must preserve the existing result-presentation boundary. The expected
output contract supplies deterministic output aliases, logical/database result
types, source-column or aggregate/bucket provenance, ordering, row limit, and
explicit time/calculation semantics. It does not prove that returned values
are business-correct, current, complete, inexpensive, or suitable for an
existing chart without separate result and chart validation.

## Verification boundary

Stage 4A unit tests can prove strict plan parsing, catalog ownership/scope
checks, type and semantic rejection, quoted-identifier construction,
parameter separation, expected-output mapping, and deterministic compilation.
They cannot prove that PostgreSQL will execute every generated statement or
that a client database remains compatible after catalog discovery.

Before connector query execution is enabled, the pending isolated live
PostgreSQL procedure must be extended with synthetic fixtures. It should cover
representative quoted identifiers, typed bound values, count and aggregate
semantics, grouping and time buckets, restricted-role privileges, TLS/egress,
timeouts/cancellation, row and byte limits, catalog/schema drift, and privilege
revocation after planning. SQLite, mocked adapters, SQL-string snapshots, and
generic SQL parsing are useful contract checks but are not evidence of live
PostgreSQL execution or security behavior.

Stage 2D real PostgreSQL/TLS verification remains pending.
