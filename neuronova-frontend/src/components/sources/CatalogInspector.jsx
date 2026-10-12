import { useCallback, useEffect, useRef, useState } from 'react'
import { sourceApi, sourceErrorCode } from '../../api/sources'
import MetricDefinitionsPanel from './MetricDefinitionsPanel'
import RelationshipDefinitionsPanel from './RelationshipDefinitionsPanel'
import AiPrivacyPanel from './AiPrivacyPanel'
import SourceProfilingPanel from './SourceProfilingPanel'

const PAGE_SIZE = 50
const INITIAL_POLL_DELAY_MS = 1_500
const MAX_POLL_DELAY_MS = 15_000
const MAX_POLL_ATTEMPTS = 40

const ACTIVE_RUN_STATES = new Set(['QUEUED', 'RUNNING'])
const TERMINAL_RUN_STATES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED'])

const SAFE_ERROR_MESSAGES = {
  authentication_required: 'Your sign-in session is unavailable. Please sign in again to view metadata.',
  connectors_disabled: 'Database metadata refresh is currently unavailable. Previously published metadata remains available below.',
  connector_egress_not_configured: 'Database metadata refresh requires administrator setup. Previously published metadata remains available below.',
  credential_key_unavailable: 'Database metadata refresh requires administrator setup. Previously published metadata remains available below.',
  catalog_not_found: 'No published metadata is available for this source yet.',
  catalog_snapshot_not_found: 'That metadata snapshot is no longer available to this source.',
  catalog_asset_not_found: 'That table is not available in the displayed metadata snapshot.',
  catalog_scope_changed: 'The selected table scope changed before metadata could be published. Refresh the source metadata again if needed.',
  source_not_found: 'This saved connection is no longer available.',
}

function safeErrorMessage(error, fallback = 'We could not load database metadata. Please try again.') {
  return SAFE_ERROR_MESSAGES[sourceErrorCode(error)] || fallback
}

function isUncertain(error) {
  return Boolean(error?.transient || error?.name === 'TimeoutError' || error instanceof TypeError)
}

function quoteIdentifier(identifier) {
  return `"${String(identifier || '').replace(/"/g, '""')}"`
}

function assetLabel(asset) {
  return `${quoteIdentifier(asset.schema_name)}.${quoteIdentifier(asset.table_name)}`
}

function dateText(value) {
  if (!value) return 'Not yet published'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Publication time unavailable' : date.toLocaleString()
}

function nullableLabel(value) {
  if (value === true || value === 'YES') return 'Yes'
  if (value === false || value === 'NO') return 'No'
  return '—'
}

function runLabel(state) {
  if (state === 'QUEUED') return 'Queued'
  if (state === 'RUNNING') return 'Synchronizing'
  if (state === 'SUCCEEDED') return 'Synchronized'
  if (state === 'FAILED') return 'Refresh failed'
  if (state === 'CANCELLED') return 'Cancelled'
  return 'Status unavailable'
}

function runMessage(run, cancellationPending) {
  if (cancellationPending && !TERMINAL_RUN_STATES.has(run?.state)) {
    return 'Cancellation requested. Waiting for the worker to confirm the final status.'
  }
  if (!run) return 'No metadata synchronization has been requested yet.'
  if (run.state === 'QUEUED') return 'A metadata refresh is queued. This does not confirm that a worker is currently running. The last published snapshot remains available while it waits.'
  if (run.state === 'RUNNING') return 'Metadata is being read from the selected database tables. The last published snapshot remains available while this runs.'
  if (run.state === 'SUCCEEDED') return 'The latest metadata refresh completed successfully.'
  if (run.state === 'FAILED') return 'The latest metadata refresh did not complete. The last published snapshot remains available when one exists.'
  if (run.state === 'CANCELLED') return 'The metadata refresh was cancelled before a new snapshot was published.'
  return 'The latest metadata synchronization status is unavailable.'
}

function hasOutOfScopeTarget(foreignKey) {
  return foreignKey.target_status === 'TARGET_OUT_OF_SCOPE'
    || foreignKey.target_status === 'target_out_of_scope'
    || foreignKey.target_in_scope === false
    || foreignKey.target_available === false
    || foreignKey.target_omitted === true
    || foreignKey.referenced_asset == null
}

function keyColumns(key) {
  const columns = key.columns || key.column_names || key.key_columns || []
  return Array.isArray(columns) ? columns : []
}

function mappingColumns(foreignKey) {
  const mappings = foreignKey.column_mappings || foreignKey.mappings || foreignKey.columns || []
  return Array.isArray(mappings) ? mappings : []
}

function mappingSourceName(mapping) {
  return mapping.referencing_column_name || mapping.source_column_name || mapping.source_column || mapping.local_column_name || mapping.from_column_name || '—'
}

function mappingTargetName(mapping) {
  return mapping.referenced_column_name || mapping.target_column_name || mapping.target_column || mapping.to_column_name || '—'
}

function DiffItems({ title, items, renderItem = item => item }) {
  if (!Array.isArray(items) || items.length === 0) return null
  return (
    <section className="catalog-diff-group">
      <h6>{title}</h6>
      <ul>
        {items.map((item, index) => <li key={`${title}-${index}`}>{renderItem(item)}</li>)}
      </ul>
    </section>
  )
}

function StructuralDiff({ diff }) {
  if (!diff) return null

  const comparisonState = diff.comparison_state || diff.state
  if (comparisonState === 'BASELINE' || comparisonState === 'baseline' || diff.has_previous_snapshot === false) {
    return <p className="catalog-empty">This is the first published metadata snapshot, so there is no prior snapshot to compare.</p>
  }
  if (comparisonState === 'SCOPE_CHANGED' || comparisonState === 'scope_changed' || diff.scope_changed) {
    return <p className="catalog-diff-note">The selected table scope changed between snapshots. A structural comparison is not shown because a missing table would not prove it was dropped from the client database.</p>
  }

  const changes = diff.changes || diff
  const tableName = item => typeof item === 'string' ? item : item.display_name || item.qualified_name || assetLabel(item)
  const columnName = item => typeof item === 'string'
    ? item
    : item.display_name || item.qualified_name || `${assetLabel(item.asset || item)}.${quoteIdentifier(item.column_name || item.name)}`
  const transition = item => {
    const label = columnName(item)
    if (item.from_type || item.to_type || item.old_type || item.new_type || item.previous_formatted_native_type || item.current_formatted_native_type) {
      const from = item.from_type || item.old_type || item.previous_formatted_native_type || 'unknown type'
      const to = item.to_type || item.new_type || item.current_formatted_native_type || 'unknown type'
      const previousNullable = item.previous_is_nullable
      const currentNullable = item.current_is_nullable
      const nullability = typeof previousNullable === 'boolean' && typeof currentNullable === 'boolean' && previousNullable !== currentNullable
        ? `; ${previousNullable ? 'nullable' : 'not nullable'} → ${currentNullable ? 'nullable' : 'not nullable'}`
        : ''
      return `${label}: ${from} → ${to}${nullability}`
    }
    if (typeof item === 'string') return item
    return label
  }
  const keyChange = item => {
    if (typeof item === 'string') return item
    const action = item.change ? `${item.change[0].toUpperCase()}${item.change.slice(1)}` : 'Changed'
    const columns = keyColumns(item).map(quoteIdentifier).join(', ') || 'No columns reported'
    return `${action} ${item.key_kind || 'key'} ${item.constraint_name || 'unnamed constraint'} on ${tableName(item)} · ${columns}`
  }
  const foreignKeyChange = item => {
    if (typeof item === 'string') return item
    const action = item.change ? `${item.change[0].toUpperCase()}${item.change.slice(1)}` : 'Changed'
    const omitted = hasOutOfScopeTarget(item)
    const target = omitted
      ? 'target omitted because it is outside the authorized scope'
      : `references ${assetLabel(item.referenced_asset)}`
    const mappings = mappingColumns(item).map(mapping => (
      `${quoteIdentifier(mappingSourceName(mapping))} → ${quoteIdentifier(mappingTargetName(mapping))}`
    )).join(', ')
    return `${action} foreign key ${item.constraint_name || 'unnamed constraint'} on ${tableName(item)} · ${target}${mappings ? ` · ${mappings}` : ''}`
  }
  const commentChange = item => {
    if (typeof item === 'string') return item
    const subject = item.column_name ? `${tableName(item)}.${quoteIdentifier(item.column_name)}` : tableName(item)
    return `Database comment changed for ${subject}`
  }

  const groups = [
    ['Tables added', changes.tables_added || changes.added_tables, tableName],
    ['Tables removed', changes.tables_removed || changes.removed_tables, tableName],
    ['Columns added', changes.columns_added || changes.added_columns, columnName],
    ['Columns removed', changes.columns_removed || changes.removed_columns, columnName],
    ['Column type or nullability changes', changes.columns_changed || changes.column_changes || changes.changed_columns || changes.type_changes, transition],
    ['Declared key changes', changes.keys_changed || changes.key_changes || changes.changed_keys, keyChange],
    ['Declared foreign-key changes', changes.foreign_keys_changed || changes.foreign_key_changes || changes.changed_foreign_keys, foreignKeyChange],
    ['Database comment changes', changes.comments_changed || changes.comment_changes || changes.changed_comments, commentChange],
  ]
  const hasChanges = groups.some(([, items]) => Array.isArray(items) && items.length > 0)

  return hasChanges ? (
    <div className="catalog-diff-groups">
      {groups.map(([title, items, renderItem]) => <DiffItems key={title} title={title} items={items} renderItem={renderItem} />)}
    </div>
  ) : <p className="catalog-empty">No bounded structural changes were reported for these published snapshots.</p>
}

function AssetDetails({ detail }) {
  const asset = detail.asset || detail
  const columns = detail.columns || asset.columns || []
  const keys = detail.declared_keys || asset.declared_keys || asset.keys || asset.key_constraints || []
  const foreignKeys = detail.foreign_keys || asset.foreign_keys || []

  return (
    <article className="catalog-asset-detail" aria-labelledby="catalog-asset-detail-heading">
      <div className="catalog-asset-detail-heading">
        <div>
          <h5 id="catalog-asset-detail-heading">{assetLabel(asset)}</h5>
          <p>Structural metadata from the displayed snapshot.</p>
        </div>
      </div>
      {asset.table_comment && (
        <section className="catalog-comment-block" aria-label="Database table comment">
          <h6>Database comment</h6>
          <p>{asset.table_comment}</p>
        </section>
      )}

      <section className="catalog-detail-section" aria-labelledby="catalog-columns-heading">
        <h6 id="catalog-columns-heading">Columns</h6>
        <div className="catalog-columns-wrap" role="region" aria-label="Ordered table columns" tabIndex="0">
          <table className="catalog-columns">
            <thead>
              <tr><th scope="col">#</th><th scope="col">Column</th><th scope="col">Native type</th><th scope="col">Nullable</th><th scope="col">Database comment</th></tr>
            </thead>
            <tbody>
              {columns.map((column, index) => (
                <tr key={`${column.ordinal_position || index}-${column.column_name || column.name}`}>
                  <td>{column.ordinal_position ?? column.ordinal ?? index + 1}</td>
                  <td>{quoteIdentifier(column.column_name || column.name)}</td>
                  <td>{column.formatted_native_type || column.native_type || column.data_type || '—'}</td>
                  <td>{nullableLabel(column.is_nullable ?? column.nullable)}</td>
                  <td>{column.column_comment || column.comment || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="catalog-detail-section" aria-labelledby="catalog-keys-heading">
        <h6 id="catalog-keys-heading">Declared primary and unique keys</h6>
        {keys.length === 0 ? <p className="catalog-empty">No declared primary or unique keys were discovered.</p> : (
          <ul className="catalog-constraint-list">
            {keys.map((key, index) => (
              <li key={key.constraint_name || key.name || index}>
                <strong>{key.constraint_name || key.name || 'Unnamed constraint'}</strong>
                <span>{key.key_kind || key.constraint_type || key.kind || key.key_type || 'Key'} · {keyColumns(key).map(quoteIdentifier).join(', ') || 'No columns reported'}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="catalog-detail-section" aria-labelledby="catalog-foreign-keys-heading">
        <h6 id="catalog-foreign-keys-heading">Declared foreign keys</h6>
        {foreignKeys.length === 0 ? <p className="catalog-empty">No declared foreign keys were discovered.</p> : (
          <ul className="catalog-constraint-list">
            {foreignKeys.map((foreignKey, index) => {
              const omitted = hasOutOfScopeTarget(foreignKey)
              return (
                <li key={foreignKey.constraint_name || foreignKey.name || index}>
                  <strong>{foreignKey.constraint_name || foreignKey.name || 'Unnamed foreign key'}</strong>
                  <span>{omitted
                    ? 'Target relationship omitted because the referenced table is outside the currently authorized scope.'
                    : `References ${quoteIdentifier(foreignKey.referenced_asset?.schema_name || foreignKey.target_schema_name || foreignKey.target_schema)}.${quoteIdentifier(foreignKey.referenced_asset?.table_name || foreignKey.target_table_name || foreignKey.target_table)}`}</span>
                  <ol className="catalog-fk-mappings">
                    {mappingColumns(foreignKey).map((mapping, mappingIndex) => (
                      <li key={`${mappingSourceName(mapping)}-${mappingTargetName(mapping)}-${mapping.ordinal_position || mappingIndex}`}>
                        {quoteIdentifier(mappingSourceName(mapping))} → {omitted ? 'target omitted' : quoteIdentifier(mappingTargetName(mapping))}
                      </li>
                    ))}
                  </ol>
                </li>
              )
            })}
          </ul>
        )}
      </section>
      <p className="catalog-definition-note">Database comments are supplied by the connected database. They are not confirmed business definitions.</p>
    </article>
  )
}

export default function CatalogInspector({ source, onClose, onSourceUpdated }) {
  const sourceId = source.source_id
  const [catalog, setCatalog] = useState(null)
  const [syncRun, setSyncRun] = useState(null)
  const [snapshotId, setSnapshotId] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [summaryError, setSummaryError] = useState(null)
  const [actionNotice, setActionNotice] = useState(null)
  const [requestingRefresh, setRequestingRefresh] = useState(false)
  const [cancellationPending, setCancellationPending] = useState(false)
  const [assetSearchInput, setAssetSearchInput] = useState('')
  const [assetSearch, setAssetSearch] = useState('')
  const [assetPage, setAssetPage] = useState(1)
  const [assetResult, setAssetResult] = useState(null)
  const [assetLoading, setAssetLoading] = useState(false)
  const [assetError, setAssetError] = useState(null)
  const [selectedAssetId, setSelectedAssetId] = useState(null)
  const [assetDetail, setAssetDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(null)
  const [showDiff, setShowDiff] = useState(false)
  const [diff, setDiff] = useState(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [diffError, setDiffError] = useState(null)

  const mountedRef = useRef(false)
  const summaryRequestRef = useRef(0)
  const assetsRequestRef = useRef(0)
  const detailRequestRef = useRef(0)
  const diffRequestRef = useRef(0)
  const snapshotIdRef = useRef(null)
  const summaryControllerRef = useRef(null)
  const assetsControllerRef = useRef(null)
  const detailControllerRef = useRef(null)
  const diffControllerRef = useRef(null)
  const actionControllerRef = useRef(null)

  const sourceStillCurrent = useCallback((requestId, ref) => (
    mountedRef.current && requestId === ref.current
  ), [])

  const resetSnapshotBrowse = useCallback(() => {
    assetsRequestRef.current += 1
    detailRequestRef.current += 1
    diffRequestRef.current += 1
    assetsControllerRef.current?.abort()
    detailControllerRef.current?.abort()
    diffControllerRef.current?.abort()
    setAssetPage(1)
    setAssetResult(null)
    setAssetError(null)
    setSelectedAssetId(null)
    setAssetDetail(null)
    setDetailError(null)
    setShowDiff(false)
    setDiff(null)
    setDiffError(null)
  }, [])

  const loadSummary = useCallback(async ({ background = false } = {}) => {
    summaryControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = summaryRequestRef.current + 1
    summaryRequestRef.current = requestId
    summaryControllerRef.current = controller
    if (!background) setSummaryLoading(true)
    try {
      const next = await sourceApi.getCatalog(sourceId, { signal: controller.signal })
      if (controller.signal.aborted || !sourceStillCurrent(requestId, summaryRequestRef)) return null
      setCatalog(next)
      setSyncRun(next.latest_run || null)
      setCancellationPending(current => current && !TERMINAL_RUN_STATES.has(next.latest_run?.state))
      const nextSnapshotId = next.current_snapshot_id || null
      if (snapshotIdRef.current !== nextSnapshotId) {
        snapshotIdRef.current = nextSnapshotId
        resetSnapshotBrowse()
      }
      setSnapshotId(nextSnapshotId)
      setSummaryError(null)
      return next
    } catch (error) {
      if (!controller.signal.aborted && sourceStillCurrent(requestId, summaryRequestRef)) {
        setSummaryError(safeErrorMessage(error))
      }
      return null
    } finally {
      if (!controller.signal.aborted && sourceStillCurrent(requestId, summaryRequestRef)) setSummaryLoading(false)
    }
  }, [resetSnapshotBrowse, sourceId, sourceStillCurrent])

  useEffect(() => {
    mountedRef.current = true
    // Start after the subscription/cleanup boundary exists. This also keeps
    // a StrictMode setup/cleanup cycle from committing an obsolete request.
    const startTimer = window.setTimeout(() => { void loadSummary() }, 0)
    return () => {
      window.clearTimeout(startTimer)
      mountedRef.current = false
      summaryRequestRef.current += 1
      assetsRequestRef.current += 1
      detailRequestRef.current += 1
      diffRequestRef.current += 1
      summaryControllerRef.current?.abort()
      assetsControllerRef.current?.abort()
      detailControllerRef.current?.abort()
      diffControllerRef.current?.abort()
      actionControllerRef.current?.abort()
    }
  }, [loadSummary])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setAssetSearch(assetSearchInput.trim())
      setAssetPage(1)
    }, 250)
    return () => window.clearTimeout(timer)
  }, [assetSearchInput])

  useEffect(() => {
    if (!snapshotId) return undefined
    assetsControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = assetsRequestRef.current + 1
    assetsRequestRef.current = requestId
    assetsControllerRef.current = controller
    const startTimer = window.setTimeout(() => {
      setAssetLoading(true)
      setAssetError(null)
      sourceApi.listCatalogAssets(sourceId, {
        snapshotId,
        page: assetPage,
        pageSize: PAGE_SIZE,
        search: assetSearch,
      }, { signal: controller.signal }).then(result => {
        if (controller.signal.aborted || !sourceStillCurrent(requestId, assetsRequestRef)) return
        // Never join an old list response to a newly displayed snapshot.
        if (result.snapshot_id !== snapshotId) {
          setAssetError('Metadata changed while the table list was loading. Reload the metadata view to continue.')
          return
        }
        setAssetResult(result)
      }).catch(error => {
        if (!controller.signal.aborted && sourceStillCurrent(requestId, assetsRequestRef)) {
          setAssetError(safeErrorMessage(error, 'We could not load tables from this metadata snapshot. Please try again.'))
        }
      }).finally(() => {
        if (!controller.signal.aborted && sourceStillCurrent(requestId, assetsRequestRef)) setAssetLoading(false)
      })
    }, 0)
    return () => {
      window.clearTimeout(startTimer)
      controller.abort()
    }
  }, [assetPage, assetSearch, snapshotId, sourceId, sourceStillCurrent])

  const pollRunId = syncRun?.run_id
  const shouldPoll = Boolean(pollRunId && (ACTIVE_RUN_STATES.has(syncRun?.state) || cancellationPending))
  useEffect(() => {
    if (!shouldPoll) return undefined
    let cancelled = false
    let timer
    let controller
    let attempts = 0

    const poll = async () => {
      if (cancelled || attempts >= MAX_POLL_ATTEMPTS) return
      controller = new AbortController()
      try {
        const next = await sourceApi.getCatalogSyncRun(sourceId, pollRunId, { signal: controller.signal })
        if (cancelled || !mountedRef.current) return
        setSyncRun(next)
        if (TERMINAL_RUN_STATES.has(next.state)) {
          setCancellationPending(false)
          if (next.state === 'SUCCEEDED') setActionNotice('Metadata synchronization completed. The published metadata view has been updated.')
          if (next.state === 'FAILED') setActionNotice('Metadata synchronization failed. Any previous published metadata remains available.')
          if (next.state === 'CANCELLED') setActionNotice('Metadata synchronization was cancelled. Any previous published metadata remains available.')
          const reconciled = await loadSummary({ background: true })
          if (!reconciled?.latest_run || reconciled.latest_run.run_id === next.run_id) {
            setSyncRun(reconciled?.latest_run && TERMINAL_RUN_STATES.has(reconciled.latest_run.state)
              ? reconciled.latest_run
              : next)
          }
          return
        }
      } catch (error) {
        const code = sourceErrorCode(error)
        if (!cancelled && (error?.status === 401 || error?.status === 403 || error?.status === 404
          || code === 'authentication_required' || code === 'source_not_found')) {
          // An ownership/access decision is terminal for this browser view;
          // do not keep probing a deleted or inaccessible source.
          setCancellationPending(false)
          setSummaryError(safeErrorMessage(error, 'This saved connection is no longer available.'))
          return
        }
        if (!cancelled && !controller.signal.aborted) {
          // A poll error does not prove a queued request was lost. Preserve the
          // displayed snapshot and retry status with a bounded backoff.
          setActionNotice('Unable to confirm synchronization status just now. The displayed metadata has not changed.')
        }
      }
      attempts += 1
      if (!cancelled && attempts < MAX_POLL_ATTEMPTS) {
        const delay = Math.min(MAX_POLL_DELAY_MS, INITIAL_POLL_DELAY_MS * (2 ** Math.min(attempts, 4)))
        timer = window.setTimeout(poll, delay)
      } else if (!cancelled) {
        setActionNotice('Synchronization is still active, but automatic status polling has paused after repeated checks. Reopen metadata later to reconcile the status.')
      }
    }
    timer = window.setTimeout(poll, INITIAL_POLL_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      controller?.abort()
    }
  }, [cancellationPending, loadSummary, pollRunId, shouldPoll, sourceId])

  const requestRefresh = async () => {
    if (requestingRefresh || shouldPoll) return
    actionControllerRef.current?.abort()
    const controller = new AbortController()
    actionControllerRef.current = controller
    setRequestingRefresh(true)
    setSummaryError(null)
    setActionNotice(null)
    try {
      const run = await sourceApi.requestCatalogSync(sourceId, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      setSyncRun(run)
      setCancellationPending(false)
      setActionNotice(run.state === 'QUEUED'
        ? 'Metadata refresh requested. It is queued until a worker claims it.'
        : 'Metadata refresh request accepted.')
      // Reconcile the catalog summary too: a coalesced request may refer to a
      // run that was already active before this browser action.
      const reconciled = await loadSummary({ background: true })
      // A post/summary race must not make the browser forget an accepted run.
      // Keep polling the returned run until the summary reflects it or reaches
      // a terminal state.
      if (!reconciled?.latest_run || reconciled.latest_run.run_id !== run.run_id) setSyncRun(run)
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        if (isUncertain(error)) {
          setActionNotice('We could not confirm whether the refresh request was accepted. Checking the current synchronization status before another request is offered.')
          await loadSummary({ background: true })
        } else {
          setSummaryError(safeErrorMessage(error, 'We could not request a metadata refresh. Please try again.'))
        }
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setRequestingRefresh(false)
    }
  }

  const cancelRefresh = async () => {
    if (!syncRun?.run_id || cancellationPending || TERMINAL_RUN_STATES.has(syncRun.state)) return
    actionControllerRef.current?.abort()
    const controller = new AbortController()
    actionControllerRef.current = controller
    setCancellationPending(true)
    setActionNotice('Cancellation requested. Waiting for the worker to confirm the final status.')
    try {
      const next = await sourceApi.cancelCatalogSync(sourceId, syncRun.run_id, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      setSyncRun(next)
      if (TERMINAL_RUN_STATES.has(next.state)) {
        setCancellationPending(false)
        const reconciled = await loadSummary({ background: true })
        if (!reconciled?.latest_run || reconciled.latest_run.run_id === next.run_id) {
          setSyncRun(reconciled?.latest_run && TERMINAL_RUN_STATES.has(reconciled.latest_run.state)
            ? reconciled.latest_run
            : next)
        }
      }
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        // Keep polling the existing run rather than suggesting that retrying
        // cancellation is harmless after an uncertain response.
        setActionNotice(isUncertain(error)
          ? 'We could not confirm cancellation yet. Checking the synchronization status.'
          : safeErrorMessage(error, 'We could not request cancellation. The synchronization status will be checked again.'))
      }
    }
  }

  const selectAsset = async assetId => {
    if (!snapshotId || detailLoading) return
    detailControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = detailRequestRef.current + 1
    detailRequestRef.current = requestId
    detailControllerRef.current = controller
    setSelectedAssetId(assetId)
    setDetailLoading(true)
    setDetailError(null)
    setAssetDetail(null)
    try {
      const result = await sourceApi.getCatalogAsset(sourceId, assetId, { snapshotId }, { signal: controller.signal })
      if (controller.signal.aborted || !sourceStillCurrent(requestId, detailRequestRef)) return
      const responseSnapshotId = result.snapshot_id || result.asset?.snapshot_id
      if (responseSnapshotId !== snapshotId) {
        setDetailError('Metadata changed while table details were loading. Select the table again from the current snapshot.')
        return
      }
      setAssetDetail(result)
    } catch (error) {
      if (!controller.signal.aborted && sourceStillCurrent(requestId, detailRequestRef)) {
        setDetailError(safeErrorMessage(error, 'We could not load table details from this metadata snapshot. Please try again.'))
      }
    } finally {
      if (!controller.signal.aborted && sourceStillCurrent(requestId, detailRequestRef)) setDetailLoading(false)
    }
  }

  const loadDiff = async () => {
    if (!snapshotId || diffLoading) return
    diffControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = diffRequestRef.current + 1
    diffRequestRef.current = requestId
    diffControllerRef.current = controller
    setShowDiff(true)
    setDiffLoading(true)
    setDiffError(null)
    try {
      const result = await sourceApi.getCatalogDiff(sourceId, { snapshotId }, { signal: controller.signal })
      if (controller.signal.aborted || !sourceStillCurrent(requestId, diffRequestRef)) return
      const responseSnapshotId = result.snapshot_id || result.current_snapshot_id
      if (responseSnapshotId !== snapshotId) {
        setDiffError('Metadata changed while structural changes were loading. Reload the comparison for the displayed snapshot.')
        return
      }
      setDiff(result)
    } catch (error) {
      if (!controller.signal.aborted && sourceStillCurrent(requestId, diffRequestRef)) {
        setDiffError(safeErrorMessage(error, 'We could not compare published metadata snapshots. Please try again.'))
      }
    } finally {
      if (!controller.signal.aborted && sourceStillCurrent(requestId, diffRequestRef)) setDiffLoading(false)
    }
  }

  const totalAssets = assetResult?.total || 0
  const pageSize = assetResult?.page_size || PAGE_SIZE
  const currentPage = assetResult?.page || assetPage
  const totalPages = Math.max(1, Math.ceil(totalAssets / pageSize))
  const run = syncRun || catalog?.latest_run
  const hasSnapshot = Boolean(snapshotId)

  return (
    <section className="source-card catalog-inspector" aria-labelledby="catalog-inspector-heading">
      <div className="source-card-heading catalog-inspector-heading">
        <div>
          <h3 id="catalog-inspector-heading">Metadata: {source.display_name}</h3>
          <p>Published structural metadata only. Client table rows are not copied into NeuroNova.</p>
        </div>
        <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={onClose}>Close metadata</button>
      </div>

      {summaryError && <p className="source-alert" role="alert">{summaryError}</p>}
      {actionNotice && <p className="source-notice catalog-status-notice" role="status">{actionNotice}</p>}

      {summaryLoading && !catalog ? <p className="source-empty" role="status">Loading metadata status…</p> : (
        <>
          <dl className="catalog-summary-fields">
            <div><dt>Published snapshot</dt><dd>{hasSnapshot ? 'Available' : 'Metadata not synchronized'}</dd></div>
            <div><dt>Published at</dt><dd>{dateText(catalog?.current_snapshot_published_at)}</dd></div>
            <div><dt>Latest sync</dt><dd>{runLabel(run?.state)}</dd></div>
          </dl>

          <p className="catalog-run-message" aria-live="polite">{runMessage(run, cancellationPending)}</p>
          <div className="source-actions catalog-actions">
            <button className="btn btn-primary" type="button" onClick={requestRefresh} disabled={requestingRefresh || shouldPoll}>
              {requestingRefresh ? 'Requesting refresh…' : shouldPoll ? 'Refresh in progress' : 'Refresh metadata'}
            </button>
            {hasSnapshot && !summaryLoading ? (
              <a
                className="btn btn-ghost-neutral"
                href={`/sources/${encodeURIComponent(sourceId)}/chat`}
                title="Start a conversation using this published metadata snapshot"
              >
                Chat with this database
              </a>
            ) : (
              <button
                className="btn btn-ghost-neutral"
                type="button"
                disabled
                title="Synchronize metadata before starting a database conversation"
              >
                Chat with this database
              </button>
            )}
            {shouldPoll && (
              <button className="btn btn-ghost-neutral" type="button" onClick={cancelRefresh} disabled={cancellationPending}>
                {cancellationPending ? 'Cancellation requested…' : 'Cancel refresh'}
              </button>
            )}
          </div>
        </>
      )}

      {!hasSnapshot && !summaryLoading ? (
        <div className="catalog-empty-state">
          <h4>Metadata not synchronized</h4>
          <p>Request a refresh to discover the currently selected database tables. A queued refresh does not by itself prove that a worker is running.</p>
        </div>
      ) : hasSnapshot && (
        <div className="catalog-browser">
          <div className="catalog-browser-heading">
            <div>
              <h4>Discovered tables</h4>
              <p>Browsing snapshot published {dateText(catalog?.current_snapshot_published_at)}.</p>
            </div>
            <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={loadDiff} disabled={diffLoading}>
              {diffLoading ? 'Comparing…' : showDiff ? 'Refresh comparison' : 'View structural changes'}
            </button>
          </div>

          <label className="source-search catalog-search">
            <span className="sr-only">Search discovered tables</span>
            <input
              type="search"
              value={assetSearchInput}
              onChange={event => setAssetSearchInput(event.target.value)}
              placeholder="Search schema or table"
            />
          </label>

          {assetResult?.scope_matches_current_selection === false && (
            <p className="source-guidance catalog-scope-notice" role="note">
              The selected table scope has changed since this snapshot. Only metadata still authorized by the current scope is shown; an absent table does not prove it was dropped from the client database.
            </p>
          )}
          {assetError && <p className="source-alert" role="alert">{assetError}</p>}
          {assetLoading && !assetResult ? <p className="source-empty" role="status">Loading discovered tables…</p> : (
            <>
              {assetResult?.items?.length ? (
                <div className="catalog-asset-list" aria-label="Discovered tables">
                  {assetResult.items.map(asset => (
                    <button
                      key={asset.asset_id}
                      className={`catalog-asset-item ${selectedAssetId === asset.asset_id ? 'active' : ''}`}
                      type="button"
                      onClick={() => selectAsset(asset.asset_id)}
                      aria-pressed={selectedAssetId === asset.asset_id}
                    >
                      <strong>{assetLabel(asset)}</strong>
                      {asset.table_comment && <span>{asset.table_comment}</span>}
                    </button>
                  ))}
                </div>
              ) : !assetLoading && <p className="catalog-empty">No discovered tables match that search in this snapshot.</p>}

              {totalAssets > pageSize && (
                <nav className="catalog-pagination" aria-label="Discovered table pages">
                  <button className="btn btn-ghost-neutral btn-sm" type="button" disabled={currentPage <= 1 || assetLoading} onClick={() => setAssetPage(current => Math.max(1, current - 1))}>Previous</button>
                  <span>Page {currentPage} of {totalPages} · {totalAssets} tables</span>
                  <button className="btn btn-ghost-neutral btn-sm" type="button" disabled={currentPage >= totalPages || assetLoading} onClick={() => setAssetPage(current => Math.min(totalPages, current + 1))}>Next</button>
                </nav>
              )}
            </>
          )}

          {detailLoading && <p className="source-empty" role="status">Loading table details…</p>}
          {detailError && <p className="source-alert" role="alert">{detailError}</p>}
          {assetDetail?.scope_matches_current_selection === false && (
            <p className="source-guidance catalog-scope-notice" role="note">
              This table detail is filtered by the current selected scope. Unavailable relationship targets are intentionally omitted.
            </p>
          )}
          {assetDetail && <AssetDetails detail={assetDetail} />}

          {showDiff && (
            <section className="catalog-diff" aria-labelledby="catalog-diff-heading">
              <h5 id="catalog-diff-heading">Structural changes</h5>
              {diffLoading && <p className="source-empty" role="status">Comparing published metadata snapshots…</p>}
              {diffError && <p className="source-alert" role="alert">{diffError}</p>}
              {!diffLoading && !diffError && diff && <StructuralDiff diff={diff} />}
            </section>
          )}
        </div>
      )}
      {!summaryLoading && catalog && (
        <>
          <MetricDefinitionsPanel
            key={`metrics:${sourceId}:${snapshotId ?? 'none'}`}
            sourceId={sourceId}
            snapshotId={snapshotId}
            selectedAssetDetail={assetDetail}
          />
          <RelationshipDefinitionsPanel
            key={`relationships:${sourceId}:${snapshotId ?? 'none'}`}
            sourceId={sourceId}
            snapshotId={snapshotId}
            selectedAssetDetail={assetDetail}
          />
          <SourceProfilingPanel
            key={`profiling:${sourceId}:${snapshotId ?? 'none'}`}
            source={source}
            sourceId={sourceId}
            snapshotId={snapshotId}
            assets={assetResult?.items || []}
            selectedAssetDetail={assetDetail}
            scopeMatchesCurrentSelection={assetResult?.scope_matches_current_selection !== false}
            onProfilingChanged={onSourceUpdated}
          />
          <AiPrivacyPanel key={`ai-privacy:${sourceId}:${snapshotId ?? 'none'}`} sourceId={sourceId} />
        </>
      )}
    </section>
  )
}
