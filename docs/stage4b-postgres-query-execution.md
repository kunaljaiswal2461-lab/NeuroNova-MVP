# Stage 4B — Controlled PostgreSQL execution

Stage 4B is an internal gateway for the single-table, structured PostgreSQL
plans defined in Stage 4A. It adds no HTTP route, natural-language planning,
chat integration, frontend feature, join support, automatic repair, or
arbitrary-SQL capability.

```text
trusted actor/source/snapshot + PostgresQueryPlan
  -> current-head authorization and deterministic compilation
  -> redacted RUNNING provenance record
  -> restricted client connection and live compatibility checks
  -> bounded cursor collection and typed serialization
  -> reauthorization before result disclosure
  -> redacted terminal provenance record
```

## Internal API

```python
result = await execute_postgres_query(
    session_factory,
    owner_user_id=trusted_jwt_user_id,
    source_id=trusted_source_id,
    snapshot_id=trusted_current_snapshot_id,
    plan=PostgresQueryPlan.model_validate(untrusted_structural_payload),
    settings=settings,
)
```

The gateway calls `validate_and_compile_postgres_query(...)` itself. It does
not accept SQL, a connection string, a caller-supplied compiled object, or a
`validated` flag. It validates before recording a run, after waiting for an
execution slot immediately before remote work, and before returning rows. A
stale snapshot is rejected rather than retargeted to a newer catalog head.
The lower-level client transport also rejects hand-built compiler artifacts;
that internal marker prevents accidental/raw application inputs but does not
replace the gateway's immediate reauthorization.

`PostgresExecutionCancellation` is an internal Stage 4C-ready handle. Calling
`await handle.cancel()` terminates the dedicated client connection. Caller
task cancellation does the same. No timeout, cancellation, ambiguous remote
outcome, or persistence failure is automatically retried.

## Client session policy

Each request uses a dedicated short-lived `asyncpg` connection. It reuses the
saved source's encrypted credential bundle, feature/egress activation check,
public-DNS destination policy, and verified TLS context. Application-database
sessions are closed/rolled back before client I/O begins.

The client session applies verified TLS; a fixed application name; default and
transaction-level read-only behavior; `READ COMMITTED`; fixed
`search_path=pg_catalog`; `TimeZone=UTC`; independently configured connect,
statement, lock, idle-transaction, and operation deadlines; and process-local
global/per-user/per-source concurrency limits. It is terminated or closed on
success, safe error, timeout, internal cancellation, and caller cancellation.

The limiter only coordinates one Python process. A multi-replica deployment
needs an external/distributed limiter before activation. Read-only alone does
not neutralize arbitrary functions, policies, extensions, or foreign tables.

## Live compatibility and relation policy

On the same client connection/transaction used for execution, the gateway
checks catalog-derived schema/table identity, live relation OID and kind,
input-column native schema/name/OID/modifier/nullability, schema/table/column
access, and prepared-statement output aliases/types.

It obtains `ACCESS SHARE` on the catalog-derived quoted relation and repeats
the relation check before prepare/cursor execution. This blocks ordinary
incompatible DDL after lock acquisition. It cannot prove that a same-name,
same-shape replacement that occurred before the lock is the historical
catalog relation, because catalog snapshots do not yet store remote OID
lineage. Runtime failures remain authoritative.

Initial support is limited to standalone ordinary base tables
(`pg_class.relkind = 'r'`) with no partition-leaf flag or inheritance
children. Views, materialized views, foreign tables, partition roots/leaves,
inherited tables, RLS-enabled tables, table-owner execution, superusers, and
`BYPASSRLS` roles are rejected. Partition behavior needs an explicit later
contract.

## Result contract and budgets

`PostgresQueryExecutionResult` is separate from the existing file/Polars
`QueryResult`. It includes source/snapshot/asset references, expected and
actual columns, visible rows, requested and safety limits, ordering,
calculation/time semantics, UTC timestamps, duration, completeness/truncation
flags, warnings, and safe failure codes.

Rows are cursor-streamed, not materialized wholesale. The compiler's requested
`LIMIT` stays intact, so safety limits are never pushed below an aggregate.
Bounded lookahead distinguishes requested top-N from a result shortened by the
safety row cap. The serialized byte cap is enforced before retaining each
complete candidate row; oversized values are discarded and marked incomplete,
never sliced or altered.

Cells are explicitly tagged and JSON-safe: integers/decimals use canonical
strings; finite/non-finite floats are distinct; UUID/date/timestamp values
are explicit ISO strings; timestamptz is UTC; and null/boolean values remain
explicit. Empty results are valid successful results. No currency or business
unit is inferred.

## Provenance

Migration `0010_postgres_query_execution` adds `postgres_query_runs`. It
stores owner/source/snapshot/asset association, the Stage 4A structural
fingerprint, lifecycle timestamps, outcome, safe failure code, visible-row
count, and completeness/truncation flags. It never stores SQL, bound values,
credentials, host/role details, raw rows, or driver messages.

A `RUNNING` record can be ambiguous after a crash or post-execution persistence
failure. It is not a queue and cannot trigger a blind rerun. If final recording
fails, completed remote rows are withheld and the result is
`execution_recording_failed`.

## Verification boundary

Unit/contract tests can verify authorization, session construction, relation
and role gates, output-shape validation, cursor lookahead, byte handling,
serialization, cancellation cleanup, concurrency release, and provenance
redaction. They cannot prove generated SQL, server read-only enforcement, TLS,
ACL/RLS behavior, locks, or cancellation against live PostgreSQL.

Before activation, extend the Stage 2D disposable PostgreSQL procedure with
quoted identifiers, typed filters, nulls, count variants, aggregates,
group/time buckets, ordering, restricted roles, schema/privilege drift,
timeout/cancellation, row/byte limits, and migration upgrades. Stage 2D
remains pending until those isolated live checks run.
