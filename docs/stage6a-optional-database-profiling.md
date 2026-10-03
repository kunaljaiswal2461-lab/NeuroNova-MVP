# Stage 6A — Optional Database Profiling and Findings

Stage 6A adds a deliberately narrow way for a source owner to request a
bounded PostgreSQL profile. It is disabled by default and is separate from
catalog discovery, database chat, metrics, relationship definitions, and the
file-analysis pipeline.

```text
owned source + current selected table scope
  -> explicit profiling opt-in
  -> manual durable profile run
  -> fixed, budgeted aggregate-only PostgreSQL read
  -> sample-qualified aggregate observations
  -> deterministic findings
  -> owner- and current-scope-filtered inspection
```

It does **not** add charts, automatic or scheduled profiling, full-table
imports, raw SQL, LLM-directed SQL, automatic business definitions, a new
connector, or changes to file DatasetContext and file-analysis pages.

## Operator workflow

1. Complete PostgreSQL source onboarding, save the selected table scope, and
   publish a compatible current catalog snapshot. Follow
   [the PostgreSQL source-onboarding guide](postgresql-source-onboarding.md)
   and [Stage 3B catalog discovery](stage3b-catalog-discovery.md) first.
2. In the source catalog experience, acknowledge that profiling reads selected
   database data and stores aggregate observations, then explicitly enable
   profiling. Enabling alone never queues, schedules, or starts a read.
3. Select a bounded subset of the tables in the source's *current* saved
   scope, and optionally a bounded subset of their supported scalar columns.
   An empty column list deliberately requests table-level observed-row count
   only; it does not mean “all columns.”
4. Request one run and inspect its queued/running/terminal state. The web
   request records work only; the worker performs remote I/O.
5. Review the completed aggregate observations and sample-qualified findings.
   Cancel a queued or running run when it is no longer wanted. Disabling
   profiling also cancels active work.

Completed observations and findings are retained when profiling is disabled.
They remain readable only to the source owner and are rechecked against the
current selected table scope on every read. Consequently, an old artifact for
a table later removed from scope is not exposed. Source deletion cascades the
durable profile records.

## Fixed sampling and measurement contract

Each selected table is read by one fixed internal aggregate statement. It has
no browser- or model-supplied SQL, identifiers, values, expressions, or
operation fragments. The only Stage 6A operation kinds are:

- observed row count;
- null and distinct counts for selected supported scalar columns; and
- count, sum, minimum, maximum, and mean for selected numeric scalar columns.

The adapter builds a PostgreSQL `TABLESAMPLE SYSTEM` population using a
server-generated `REPEATABLE` seed and a bounded `LIMIT`, then materializes
that population in one CTE. Every measure for a table is computed from that
same CTE, so null counts, distinct counts, and numeric summaries share one
consistent observed population.

The initial defaults are a 1% `SYSTEM` sample and at most 5,000 observed rows.
Configuration cannot exceed a 10% sample or 10,000 observed rows. `SYSTEM`
sampling is block-based, not independent-row random sampling, so it must not
be described as representative of the whole table. A `LIMIT` bounds the
retained sample population but does not by itself bound database work;
connection, statement, lock, idle-transaction, and overall operation
deadlines, bounded table/column/operation selections, and worker concurrency
also apply. If sampling is unavailable or insufficient, the run fails or
records the bounded result—it never silently falls back to an unrestricted
full-table scan.

The configured sampling percent is persisted and used with at most four
decimal places; configuration with finer precision is rejected rather than
silently rounded to a different remote sampling parameter.

The run and table observations record the method, percent, limit, seed,
collection time, observed-row count, status, warning codes, and the
`OBSERVED_SAMPLE` population label. Every supported measure is exact only for
that observed sample, not for the complete table. In particular:

- sample distinct counts are not whole-table uniqueness claims;
- zero observed nulls do not prove a column has no table-wide nulls;
- profile time is collection time, not business-data freshness; and
- independently sampled populations are never combined as though they were a
  single sample.

The supported initial types are current `pg_catalog` scalar columns:
`int2`, `int4`, `int8`, `numeric`, `float4`, `float8`, `bool`, `uuid`, `date`,
`timestamp`, `timestamptz`, `text`, `varchar`, and `bpchar`. Unsupported
columns are rejected before remote work. Text values, example rows, top
values, text extrema, SQL text, credentials, driver messages, and raw sampled
rows are not persisted or returned. Numeric summaries are retained only as
aggregate values for supported numeric columns.

## Budgets and durable worker behavior

The profile request is constrained before it enters the durable queue. Default
limits are 10 tables per run, 25 columns per table, 100 columns per run, and
250 logical aggregate operations per run. The row-count measure costs one
operation per table; null/distinct measures cost two per selected scalar
column; a numeric summary adds one. Deployments may narrow these validated
limits, sampling bounds, or deadlines but cannot configure a sample above the
hard 10%/10,000-row transport ceiling.

Profiling reuses the durable catalog worker process rather than adding another
worker framework. From `backend/`, apply migrations and run the web process
and shared worker as separately supervised processes:

```bash
alembic upgrade head
uvicorn main:app --host 0.0.0.0 --port 8000
python -m app.workers.catalog_discovery_worker
```

The worker requires the same activated connector, egress-policy, encrypted
credential, and verified-TLS configuration as catalog discovery. It does not
start automatically in the web process. By default it processes one profile
job concurrently, uses a 45-second renewable lease, attempts a transient run
at most twice, and backs off from 5 to 30 seconds. The remote adapter uses a
dedicated restricted client connection with default read-only behavior,
catalog-derived live relation/column checks, relation locking, role/RLS
restrictions, and default 5s connect, 8s statement, 2s lock, 10s
idle-transaction, and 15s overall operation deadlines.

Durable run rows bind the request to the source, owner, current catalog
snapshot, current-scope fingerprint, selected asset/column IDs, sampling
configuration, and profile configuration generation. Workers recheck that
binding before credentials are opened, while renewing their lease, and before
publication. A lost lease, cancellation, opt-out, source deletion, scope
change, catalog incompatibility, or superseding request fences the worker from
publishing. Cancellation terminates the dedicated remote connection; failed
and retried runs retain only safe category codes, never remote error text.

## API and source UI contract

All Stage 6A HTTP routes are JWT-only owner routes below
`/api/v1/sources/{source_id}`. API-key-only access is rejected, and missing or
unowned sources/runs retain the existing non-enumerating `404` behavior.

| Method and path suffix | Purpose |
| --- | --- |
| `PUT /profiling` | Set `{ "profiling_enabled": true|false }`; it never starts a run. |
| `POST /profile-runs` | Create/coalesce one manual bounded request. The body contains only current catalog `asset_id` and optional `column_ids`; it accepts no SQL or table names. Returns `202`. |
| `GET /profile-runs` | Page through owned durable runs. |
| `GET /profile-runs/{run_id}` | Read one owned run status and safe provenance. |
| `POST /profile-runs/{run_id}/cancel` | Request cancellation of an owned queued/running run. |
| `GET /profile-runs/{run_id}/observations` | Read paginated table and column aggregate observations. |
| `GET /profile-runs/{run_id}/findings` | Read paginated deterministic findings. |

Manual requests require connector credential operations to be available, but
status and historical reads do not initiate remote work. The source view
surfaces opt-in state, run status and cancellation, sample coverage and
limits, aggregate observations, deterministic findings, and safe failure or
unsupported-status information. It uses plain text and bounded pagination;
Stage 6A intentionally adds no visual charting.

## Persistence and findings

Migration `0014_optional_source_profiling` adds the source profiling
configuration generation plus source-scoped durable run, selected asset/column, table
observation, column observation, and finding records. These records are
separate from catalog structure, semantic suggestions, confirmed metrics,
relationships, and file-profile artifacts.

Findings are deterministic and link to their supporting table and column
observations. The initial set is deliberately small:

- an observed null-rate statement, such as “12% of observed rows had a null
  value”; and
- an observed constant statement when exactly one non-null value occurred in
  the observed sample.

Each includes the observed-population limitation. Neither finding creates or
changes a metric, relationship, semantic definition, or business rule.

## Verification boundary

Automated backend, API, worker, adapter, and frontend tests cover the durable
and aggregate-only contracts with synthetic/mocked fixtures: default-off
behavior, owner/scoped authorization, selection narrowing, no arbitrary SQL,
sampling labels and caps, empty/all-null/unsupported cases, deterministic
findings, lease fencing, cancellation, opt-out, and historical scope filtering.
They do not prove live PostgreSQL sampling cost or remote cancellation.

Before activating profiling outside an isolated test environment, extend the
disposable live PostgreSQL procedure in
[Stage 2D](stage2d-postgresql-integration.md) with synthetic data and record
only safe identifiers/results. The remaining live checks include `SYSTEM`
sampling behavior and cost under skew, empty samples, statement/lock/operation
timeouts, TLS/ACL/RLS and restricted-role gates, relation/schema drift,
connection termination on cancellation, durable lease recovery/fencing, source
scope/opt-out/deletion races, migration upgrade behavior, and rendered browser
inspection. Those live PostgreSQL and browser checks remain pending until run
in that isolated environment.
