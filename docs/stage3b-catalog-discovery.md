# Stage 3B durable PostgreSQL catalog discovery

Stage 3B adds a durable, manual discovery path for a saved PostgreSQL source:

```text
saved source + explicit table scope
  -> catalog synchronization request
  -> separately supervised worker
  -> scoped structural discovery
  -> immutable catalog snapshot publication
```

It is not a query engine. It does not fetch table rows, samples, value
distributions, profiles, embeddings, inferred relationships, semantic
definitions, or database-chat results. File upload, Polars execution, and
file chat are unchanged.

Stage 3C adds the owner-scoped UI and HTTP read contracts for inspecting a
published snapshot and comparing safe successive snapshots. See the
[Stage 3C catalog-inspection guide](stage3c-catalog-inspection.md); it does
not change this worker's discovery boundary.

## Deployment and startup

Apply migrations, run the web process, and run the discovery worker as
separate supervised processes from `backend/`:

```bash
alembic upgrade head
uvicorn main:app --host 0.0.0.0 --port 8000
python -m app.workers.catalog_discovery_worker
```

The worker uses the existing application PostgreSQL database (`DATABASE_URL`)
as its durable queue. It does not use Redis or another queue service. A web
process may accept a request while no worker is running; the response remains
`QUEUED` until an appropriately configured worker starts.

The worker must receive the same connector configuration as the web process:

```dotenv
CONNECTORS_ENABLED=true
CONNECTOR_EGRESS_POLICY_CONFIGURED=true
CONNECTOR_CREDENTIALS_ACTIVE_KEY_ID=<operator-managed-key-id>
CONNECTOR_CREDENTIALS_ACTIVE_KEY=<Fernet-key>
```

It exits before claiming work when these settings or credential protection are
unavailable. This does not affect file-only web startup, because the web
process does not start a worker implicitly. Connector use must remain disabled
until the egress and TLS prerequisites in
[the source-onboarding guide](postgresql-source-onboarding.md) are met.

## Worker behavior

`catalog_sync_runs` holds the request, attempt count, lease owner/token,
expiry, next retry time, and cancellation request. Workers claim source rows
with PostgreSQL row locking and `SKIP LOCKED`; this enforces one active worker
per source without a process-local lock.

- A claim has an opaque lease token and a finite expiry. Long discovery
  renews it periodically.
- Startup and each renewal recheck the lease. A cancelled or replaced worker
  does not decrypt credentials, start discovery, publish, renew, or finalize.
- Publication and failure finalization validate the token in their short final
  application-database transaction. An expired or replaced worker is fenced
  even if it completed client-side work.
- Transient failures retry with bounded exponential delays until
  `CATALOG_WORKER_MAX_ATTEMPTS`; credential, access/scope, TLS/policy,
  metadata-limit, and local configuration errors are terminal.
- A worker stopped during remote work cancels its local task and leaves the
  lease for expiry/recovery. A source/scope change, deletion, cancellation,
  or lost lease prevents publication. The prior current snapshot remains on
  a failed or cancelled run.
- Repeated manual requests for an unchanged active source scope return the
  existing queued/running run. A changed scope marks a stale active run as
  `FAILED` with `scope_changed` and creates one fresh request.

Important settings (all have bounded validation in `Settings`) are:

| Setting | Purpose |
| --- | --- |
| `CATALOG_WORKER_MAX_CONCURRENT_JOBS` | Per-process maximum simultaneous discoveries. |
| `CATALOG_WORKER_POLL_INTERVAL_SECONDS` | Queue poll interval when no claimable run exists. |
| `CATALOG_WORKER_LEASE_SECONDS` / `CATALOG_WORKER_LEASE_RENEWAL_SECONDS` | Claim expiry and heartbeat cadence. |
| `CATALOG_WORKER_MAX_ATTEMPTS` / retry settings | Retry ceiling and exponential delay bounds. |
| `CATALOG_DISCOVERY_TIMEOUT_SECONDS` | Whole discovery operation bound. |
| `CATALOG_DISCOVERY_MAX_*` | Tables, columns, relationships, and metadata-byte limits. |

## Discovery contract and limits

The adapter opens a short-lived TLS PostgreSQL connection using the existing
connection policy and decrypted, source-bound credential bundle. In a
read-only `REPEATABLE READ` transaction, it first rechecks every selected
table's current schema/table privileges. Each metadata query is restricted to
the selected `(schema_name, table_name)` pairs through bound array parameters;
no user-controlled identifier is interpolated into SQL.

It extracts only table and column identity/comments, ordered columns and
native type details, nullability, declared primary/unique keys, and declared
foreign-key column mappings. Discovery aborts rather than silently publishing
a partial result when a selected table is unavailable, metadata is incomplete,
or a configured bound is exceeded. References to excluded tables are recorded
only as the Stage 3A out-of-scope omission status; target names and columns
are not retained.

Comments are stored as descriptive source content, never as application or
LLM instructions. Safe failure codes only are persisted or returned; secrets,
driver messages, SQL parameter values, and metadata comments are not logged.

## JWT-only HTTP contract

All endpoints require a valid Bearer JWT and prove source ownership. The
global legacy `X-API-Key` is deliberately rejected and does not confer access
to a source, a run, a snapshot, or connector credentials.

| Method and path | Success | Response |
| --- | --- | --- |
| `POST /api/v1/sources/{source_id}/catalog-syncs` | `202` | Persistent/coalesced run: `run_id`, state, times, attempt fields, next retry, safe failure code. |
| `GET /api/v1/sources/{source_id}/catalog` | `200` | Latest run plus current snapshot ID/time and whether an older snapshot remains available. |
| `GET /api/v1/sources/{source_id}/catalog-syncs/{run_id}` | `200` | Owned run's safe status fields. |
| `POST /api/v1/sources/{source_id}/catalog-syncs/{run_id}/cancel` | `200` | The cancelled owned run. |

Unowned/missing sources and runs use the existing non-enumerating `404`
behavior. Missing, invalid, or API-key-only authentication is `401`. A manual
request is `503` rather than queued when connector activation or credential
protection is unavailable; read/status routes remain owner-scoped. There is no
HTTP endpoint that accepts catalog metadata or waits for discovery.

## Stage 2D required live verification

This repository's local test environment does not provide the isolated TLS
PostgreSQL topology required for live Stage 2D verification. Before enabling
connectors outside an isolated test environment, extend and execute
[the Stage 2D procedure](stage2d-postgresql-integration.md) with synthetic
data to prove PostgreSQL migration/upgrade behavior, `SKIP LOCKED` claiming,
lease recovery/fencing, privilege visibility, timeout cleanup, source/scope
races, and the assertion that no client rows are read. Record only safe test
identifiers and error codes.

The inherited compatibility caveat remains: if an existing application
database contains `CATALOG_READY` in `source_status_enum` from the earlier
unreleased `0007_postgres_sources` migration, stop and add a forward
compatibility migration before applying this branch. Editing the historical
migration cannot repair an already-applied database.
