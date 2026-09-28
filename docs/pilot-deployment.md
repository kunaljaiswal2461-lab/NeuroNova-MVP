# Free-tier pilot deployment

This mitigation needs no paid infrastructure, external scheduler, or profiling
algorithm changes. It reduces load; it does not make in-process jobs durable.

## Required backend environment

Set these exact values on the existing Render backend service, as well as in
any local `.env` used for the pilot:

```dotenv
MAX_CONCURRENT_PIPELINES=1
MAX_UPLOAD_BYTES=20971520
MAX_REQUEST_BYTES=22020096
```

The upload limit is **20 MiB** (20 × 1024 × 1024 bytes), displayed as “20 MB”
in the UI. Exactly that many file bytes are allowed. The **21 MiB** request
cap leaves room for multipart headers. All three settings must be positive.
The frontend pilot file limit is fixed: keep the backend values aligned.
The existing request middleware checks declared `Content-Length`; actual file
bytes are also enforced while saving, even when that header is absent. This
change does not add a streaming cap to the multipart parser itself.

Existing Render environment values override code defaults. Remove/replace old
`MAX_UPLOAD_BYTES=104857600`, old request caps, or concurrency values of 3;
changing the tracked examples alone does not change an existing service.
Apply changes through Render's environment configuration and redeploy. No
production configuration or database data was changed by this code change.

Run **one Uvicorn worker and one service instance**, for example from `backend/`:

```sh
uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
```

Do not use reload in deployment. The semaphore is per process, not a distributed
lock: multiple workers/replicas each allow their own active pipeline. A second
accepted upload remains `QUEUED` while the first owns the existing semaphore.
This is not a durable queue and there is no automatic retry/replay of uploads.

## Frontend deployment

Keep `VITE_API_BASE_URL=https://<your-render-service>.onrender.com/api/v1`
configured for both Production and Preview environments in Vercel, then rebuild
the frontend. Vercel's current `vercel.json` only routes to `index.html`; it does
not proxy API requests. The relative `/api/v1` URL used by the local Vite preview
depends on its development proxy and must not replace this deployed setting.
Check that `/api/v1/health` on the configured backend returns JSON with
`"status": "ok"`, not merely an HTTP 200 HTML app shell.

## Interrupted profiling cleanup

An application-lifespan task starts a DB-only reconciliation pass at startup
and repeats every 60 seconds. Each pass updates at most 100 rows, with a
10-second operation timeout; exceptions are logged and retried next pass.
Shutdown cancels/awaits the task before disposing the database engine.

Only `PROFILING` rows whose `updated_at` is **strictly older than 20 minutes**
become `FAILED`, with this message:

> Processing was interrupted. Please upload the dataset again.

Status and age are checked again in the same atomic conditional update, so a
row refreshed or advanced before the update is not overwritten. Fresh rows,
exactly-20-minute-old rows, `QUEUED`, `COMPLETE`, existing `FAILED`, and all other
stages are unchanged. Large backlogs take multiple passes. Sleeping free-tier
services cannot run cleanup until they wake; database outages delay cleanup.

**Liveness caveat:** profiling timestamps advance at stage boundaries, not via
a heartbeat. A genuinely live stage taking over 20 minutes can be classified
as interrupted; the unchanged pipeline may subsequently advance its status
again. Conditional updates avoid clobbering already-recent rows but cannot
prove worker death. Removing this ambiguity needs leases/heartbeats or durable
job ownership, intentionally outside this no-profiling-logic-change mitigation.
Lost `QUEUED` jobs or jobs interrupted in other stages are not reconciled here.

**Storage caveat:** local files on Render's ephemeral filesystem may disappear
after restart/redeploy even though Postgres metadata survives. This change does
not restore missing artifacts or fail old `COMPLETE` rows. Ask pilot users to
reupload affected datasets; durable storage/queues remain future work. A 20 MiB
compressed/wide dataset can still use substantially more memory during parsing
and analysis, so the file limit and one active pipeline reduce, not eliminate,
free-tier memory pressure.

## Post-deploy checks (operator-run)

1. Confirm the three environment values above and the single-worker command.
2. Upload a small valid dataset and verify it reaches `COMPLETE`.
3. Upload two small datasets together: one profiles while the other is queued,
   and both eventually complete without parallel profiling.
4. Confirm files over 20 MiB are rejected before frontend POST, and direct API
   uploads over the file limit receive HTTP 413 with a clear size error.
5. Verify interrupted-status behavior using disposable test data, not existing
   users' records. No production mutation is required for automated tests.
