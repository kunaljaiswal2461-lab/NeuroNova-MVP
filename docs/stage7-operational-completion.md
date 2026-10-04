# Stage 7 — Operational Completion

Stage 7 reconciles the connector implementation through Stage 6B with the
target architecture. It closes lifecycle and operational gaps without adding
a connector, a query language, automatic profiling, vector retrieval, or any
change to uploaded-file analysis and chat.

## Implementation matrix

| Architecture concern | Delivered state | Stage 7 reconciliation |
| --- | --- | --- |
| Owner-bound PostgreSQL source, encrypted credential, explicit scope | Existing source/credential/scope models | Reused. Saved endpoint identity remains immutable. |
| Test, save, discover, refresh, revoke lifecycle | Onboarding, deletion, manual durable discovery existed | Added saved-source retest, credential replacement, scope update, opt-in structural refresh. |
| Durable metadata worker and immutable snapshot head | Existing durable run, lease and snapshot workflow | Reused for schedules; no second scheduler or queue. |
| Scope-constrained metadata/profiles/definitions | Existing catalog, profile, metric and relationship fences | Configuration generation fences source changes and active work. |
| Controlled NL planning/execution | Existing bounded plan/compiler/gateway | Added exactly one malformed-envelope correction before compilation. |
| Typed results/charts | Existing Stage 4B/6B contracts | Reused; scope removal redacts typed source results in transcripts. |
| Operational visibility | Existing source/run state | Added refresh policy/next due, last success, and safe latest failure. |

## Saved-source maintenance

All routes are JWT-only and source-owner scoped. A source ID, run ID, or
snapshot ID is never an authorization capability. No route accepts SQL, raw
metadata, a new host, port, or database name.

| Method and suffix | Purpose |
| --- | --- |
| `POST /api/v1/sources/{source_id}/retest` | Decrypt the current protected credential, test the immutable endpoint, and return names-only eligible tables plus safe source state. |
| `PUT /api/v1/sources/{source_id}/credentials` | Test proposed credentials/TLS against the immutable endpoint, then atomically replace the encrypted bundle. |
| `PUT /api/v1/sources/{source_id}/scope` | Retest the saved connection, validate explicit names-only selected tables, and atomically replace scope. |
| `PUT /api/v1/sources/{source_id}/catalog-refresh` | Enable/disable a 15-minute to 7-day structural refresh interval; it never starts remote work in the web process. |

The source UI exposes the same narrow controls after “View selected tables”:
retest, credential rotation, revalidated table selection, and a fixed schedule
chooser. Passwords and replacement CA input stay only in browser form state
for the request and are not rendered after success.

Credential replacement and changed scope increment `configuration_generation`
and cancel queued or leased catalog/profile work. The executor captures that
generation before its remote connection and refuses to disclose a completed
result if it changed. Scope removal clears the typed PostgreSQL result blob
from source-bound chat messages; prose remains, but result rows do not.
Source deletion retains its database-side cascade through source, catalog and
profile records, source-bound sessions, and messages.

Existing catalog, profile, metric, relationship, and query reads recheck the
current scope/snapshot. A scope expansion needs compatible metadata before
the added relation can be queried; a scope removal cannot revive stale
metadata, definitions, profiles, or retained result rows.

## Scheduled structural refresh

Scheduling is **off by default** and applies only to catalog structure. It
does not run profiling, definition inference, client-row imports, LLM work,
or browser-side jobs. The separately supervised
`catalog_discovery_worker` materializes due sources as ordinary durable
`catalog_sync_runs`, capturing scope and source configuration generation.
There is at most one active run per source. Downtime does not replay every
missed interval. Disabling policy prevents future due work and cancels queued
or leased *scheduled* jobs without cancelling an unrelated manual refresh.

From `backend/`, run the web process and worker separately:

```bash
alembic upgrade head
uvicorn main:app --host 0.0.0.0 --port 8000
python -m app.workers.catalog_discovery_worker
```

The worker needs the same connector activation, egress acknowledgement, and
credential keys as the web service. Migration
`0015_stage7_source_operations` adds only lifecycle/schedule provenance; it
does not decrypt, move, rewrite, or delete credentials, snapshots, profile
observations, definitions, chat messages, or client data.

## Exact repair policy

One request-local `PostgresPlanRepairBudget` permits **one** correction only
when the LLM response cannot be parsed as the required generic planning
envelope, including an unknown discriminator. The correction receives the
same pinned context and a sanitized structural instruction; it receives no
driver error, raw client result, credential, or SQL.

The budget is consumed before compilation or remote work. It never applies to
a rejected asset/filter/join, definition change, clarification, unsupported
request, execution error, cancellation, timeout, result-shape check,
credential/source/scope change, or completed client operation. There is no
database rerun and no repair chain.

## Deployment, concurrency, and liveness limits

The durable queue/lease state coordinates catalog workers through the
application database, but connector outbound concurrency and controlled-query
limits remain process-local. The supported topology is a separately
supervised worker plus web process(es), with documented conservative egress
limits. Do not horizontally scale connector execution traffic until a
distributed limiter, cross-replica cancellation design, and operational
monitoring are introduced and tested.

The API reports safe source state, last test, refresh policy/next due, last
catalog success, and a safe failure category. It intentionally does **not**
claim a worker is alive because a source is enabled. Worker liveness/restart
alerts are currently the deployment supervisor’s responsibility; this is an
explicit unresolved decision, not an inferred health signal.

## Storage and retention facts

`STORAGE_BACKEND=local` persists file-pipeline artifacts under the deployment
filesystem/volume configured by `DATA_DIR`; Docker Compose uses
`neuronova_data`. This is local deployment storage, not a durable
multi-replica/object-store guarantee. Catalog snapshots, run provenance,
confirmed definition history, and aggregate-only profile records currently
have no automatic retention/deletion scheduler. Source/account deletion
cascades remain the implemented removal paths.

No retention policy was added: retention windows, backup policy, legal holds,
object-store lifecycle, and safe archive/purge behavior are product and
operations decisions absent from this repository. Decide and test them before
production rollout. Local `output/` artifacts are developer-local and are not
deployment storage.

## Verification completed and still required

Completed locally:

- full backend suite, including safe retest, due scheduled enqueue, and one
  malformed-envelope correction: **389 passed**;
- full frontend suite: **113 passed**;
- frontend production build and scoped lint; and
- Python compilation for application and planner modules.

These checks use mocked/synthetic connectors. They do not prove live customer
database behavior, browser rendering against a running API, PostgreSQL
migration upgrade, process-supervisor restarts, or durable-volume behavior.
An isolated SQLite full-upgrade smoke test stops at the pre-existing
`0005_auth_tables` SQLite `ALTER` limitation before it reaches Stage 7; that
does not validate or invalidate the PostgreSQL-targeted `0015` migration. The
repository’s UI test command is `npm run test:ui`; `npm test` is not a defined
script.

## Stage 8 acceptance checklist

Stage 8 is not started by this change. Before accepting a future production
operational stage, verify all of the following in an isolated synthetic
PostgreSQL environment and the intended deployment topology:

1. Apply `0015_stage7_source_operations` on a PostgreSQL clone and prove the
   approved downgrade/restore plan before production migration.
2. Retest, rotate credentials, and change scope while catalog, profile, and
   controlled-query work are queued/leased; prove stale work cannot publish or
   disclose rows.
3. Enable, disable, restart, and recover scheduled refreshes across worker
   restarts and lease expiry; prove no duplicate source work.
4. Test TLS, ACL/RLS, restricted role, timeout, cancellation, schema drift,
   and safe error categories with no secret/SQL/result logging.
5. Verify retention, backup/restore, source/account deletion, and local
   versus durable volume/object-store behavior against an approved policy.
6. Establish worker liveness/restart alerting and decide whether distributed
   concurrency/cancellation coordination is needed before replica scaling.
7. Perform authenticated browser checks for maintenance actions, freshness,
   stale definitions, transcript redaction, typed results, and charts.
