# Stage 3C catalog inspection and structural changes

Stage 3C makes the immutable PostgreSQL catalog snapshots from Stages 3A/3B
inspectable from the existing **Database** source experience. It does not add
database chat, analytical SQL, profiling, semantic definitions, embeddings,
scheduled refresh, or another worker. File upload, file-backed Polars
execution, and file chat remain separate and unchanged.

```text
saved PostgreSQL source
  -> published structural snapshot
  -> owner-scoped table browse/detail reads
  -> optional manual refresh + durable-worker status
  -> deterministic comparison with the prior published snapshot
```

## User flow

Each saved PostgreSQL source has a **View metadata** action. The view shows
the source name, current publication time, latest synchronization state, and
manual refresh/cancellation controls. A source with no current snapshot is
shown as **Metadata not synchronized**; it is never described as ready for
database chat.

The table list is paginated and search is limited to selected, discovered
table identities. Selecting a table loads only that table's ordered columns,
native types, nullability, database comments, declared primary/unique keys,
and foreign-key mappings. Database comments are rendered as text and labelled
as database-supplied descriptions, not confirmed business definitions.

The browser pins all list, detail, and diff requests to the displayed
`snapshot_id`. A refresh or failed run leaves the prior published snapshot
visible. Status polling uses bounded exponential backoff, stops at terminal
states/unmount/lost access, and ignores stale responses after source or
snapshot changes. A queued status means only that work is queued; it does not
claim that a worker is running.

## JWT-only HTTP contract

All paths below require a valid Bearer JWT and source ownership. A global
legacy `X-API-Key` is rejected with `401`; a source, snapshot, asset, or run
ID is not an authorization capability. Missing or unowned resources use the
existing non-enumerating behavior.

The pre-existing status paths remain unchanged:

| Method and path | Success | Purpose |
| --- | --- | --- |
| `GET /api/v1/sources/{source_id}/catalog` | `200` | Latest safe run status plus the current snapshot ID/time. |
| `POST /api/v1/sources/{source_id}/catalog-syncs` | `202` | Request or coalesce a manual refresh. |
| `GET /api/v1/sources/{source_id}/catalog-syncs/{run_id}` | `200` | Read one owned run's safe status. |
| `POST /api/v1/sources/{source_id}/catalog-syncs/{run_id}/cancel` | `200` | Cancel an owned active run. |

Stage 3C adds these bounded read contracts:

| Method and path | Required query | Success response |
| --- | --- | --- |
| `GET /api/v1/sources/{source_id}/catalog/assets` | `snapshot_id`; optional `page` (1–10,000), `page_size` (1–100), and literal `search` (≤128 UTF-8 bytes) | `source_id`, `snapshot_id`, `snapshot_published_at`, `scope_matches_current_selection`, pagination fields, and `items`. Each item has `asset_id`, separate `schema_name`/`table_name`, and optional `table_comment`. |
| `GET /api/v1/sources/{source_id}/catalog/assets/{asset_id}` | `snapshot_id` | Pinned snapshot metadata with `asset`, ordered `columns`, `declared_keys`, and `foreign_keys`. Columns retain native fields and comments; keys and mappings retain ordinal order. |
| `GET /api/v1/sources/{source_id}/catalog/diff` | `snapshot_id` | A deterministic bounded comparison with `state`, prior snapshot identity/time when safe, `truncated`, `change_limit`, and arrays for table, column, key, foreign-key, and comment changes. |

`foreign_keys` and `foreign_keys_changed` use `target_status`. For
`TARGET_OUT_OF_SCOPE`, `referenced_asset` is `null` and `column_mappings` is
empty: the response never supplies an excluded target's identity or columns.
Identifiers remain separate fields rather than ambiguous dotted strings.

## History and structural comparisons

Snapshot records remain immutable internally. Public historical reads apply
the source's *current* selected scope again. Consequently, an old asset that
is no longer selected is not listed or detailed, and a formerly visible
foreign-key target is redacted.

The diff compares the requested published snapshot to the preceding published
snapshot for that source, ordered by the durable run sequence. It only returns
a comparison when the captured scope fingerprints match the current selected
scope and each other. Otherwise it returns `state: "scope_changed"` with no
structural detail, because a missing asset would not prove a client database
table was dropped. The first published snapshot returns `state: "baseline"`.

For a safe comparison, `state: "compared"` reports additions/removals by
stable `(schema_name, table_name)` identity, column identity/type/nullability,
declared key changes, foreign-key mapping changes, and comment changes. It
does not infer or label renames. The response is capped at 500 changes across
all groups and sets `truncated` when the cap is reached.

## Operations and limitations

No new migration is required for Stage 3C: it reads the existing immutable
catalog persistence introduced in Stage 3A. Deploying the updated backend and
frontend is still required before this interface and its routes become live;
this stage does not deploy anything.

The separately supervised Stage 3B worker remains responsible for discovery.
Stage 2D's isolated real-PostgreSQL/TLS/migration verification remains
pending, so mocked discovery/status and SQLite tests do not prove live client
database behavior. Follow the Stage 3B worker runbook and the Stage 2D
procedure before enabling the connector outside its isolated test topology.
