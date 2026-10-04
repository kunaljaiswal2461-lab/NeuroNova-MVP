import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { sourceApi, sourceErrorCode } from '../../api/sources'
import { DatabaseChartRenderer } from '../charts/DatabaseChartRenderer'
import { buildProfileNullRateChartSpecs } from '../charts/databaseChartSpec'

const RUN_PAGE_SIZE = 20
const RESULT_PAGE_SIZE = 100
const INITIAL_POLL_DELAY_MS = 1_500
const MAX_POLL_DELAY_MS = 15_000
const MAX_POLL_ATTEMPTS = 40
const CLIENT_MAX_TABLES_PER_RUN = 10
const CLIENT_MAX_COLUMNS_PER_TABLE = 25

const ACTIVE_RUN_STATES = new Set(['QUEUED', 'RUNNING'])
const TERMINAL_RUN_STATES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED'])

// This intentionally mirrors only the small, ordinary scalar family that the
// initial database profiler can observe. The server remains authoritative and
// rejects a stale or unsupported catalog identity before a run is created.
const SUPPORTED_SCALAR_TYPES = new Set([
  'bool',
  'int2', 'int4', 'int8', 'numeric', 'float4', 'float8',
  'text', 'varchar', 'bpchar', 'uuid',
  'date', 'timestamp', 'timestamptz',
])

const SAFE_ERROR_MESSAGES = {
  authentication_required: 'Your sign-in session is unavailable. Please sign in again to manage database profiling.',
  source_not_found: 'This saved connection is no longer available.',
  catalog_not_found: 'Publish current metadata before configuring a database profile.',
  catalog_snapshot_not_found: 'The displayed metadata snapshot is no longer available. Refresh metadata and try again.',
  catalog_scope_changed: 'The selected table scope changed. Refresh metadata before profiling.',
  catalog_asset_not_found: 'One selected table is no longer available in the current authorized metadata.',
  profiling_not_enabled: 'Enable optional database profiling before requesting a profile run.',
  profile_catalog_unavailable: 'Publish current authorized metadata before requesting a database profile.',
  invalid_profile_request: 'Choose current authorized tables and supported columns before requesting a profile.',
  profile_run_state_invalid: 'That profile run is no longer in a state that allows this action.',
  profile_run_lease_lost: 'The worker lease changed before this profile could finish. Refresh the run status before requesting another profile.',
  profile_run_superseded: 'The source scope, catalog, or profiling configuration changed before this profile could publish. Refresh metadata before trying again.',
  source_profiling_disabled: 'Enable optional database profiling before requesting a profile run.',
  profiling_disabled: 'Enable optional database profiling before requesting a profile run.',
  profile_run_not_found: 'That database profile run is no longer available.',
  profile_run_not_cancellable: 'That profile run can no longer be cancelled.',
  profile_selection_invalid: 'Choose current authorized tables and supported columns before requesting a profile.',
  profile_selection_limit_exceeded: 'The selected profile scope exceeds a server-enforced table, column, or operation budget. Narrow the selection and try again.',
  profile_snapshot_changed: 'Metadata changed while the profile was being prepared. Refresh metadata and choose the current catalog again.',
  profile_config_conflict: 'Profiling configuration changed while this request was being prepared. Refresh the panel and try again.',
  profile_persistence_failed: 'The profile request could not be saved. No database profiling was started.',
}

function safeErrorMessage(error, fallback = 'We could not complete that database profiling request. Please try again.') {
  return SAFE_ERROR_MESSAGES[sourceErrorCode(error)] || fallback
}

function isUncertain(error) {
  return Boolean(error?.transient || error?.name === 'TimeoutError' || error instanceof TypeError)
}

function quoteIdentifier(identifier) {
  return `"${String(identifier || '').replace(/"/g, '""')}"`
}

function assetId(asset) {
  // The catalog UUID is the only browser-to-server identity. Display names
  // must never become a hidden authorization or execution parameter.
  return asset?.asset_id ?? asset?.id ?? null
}

function columnId(column) {
  return column?.column_id ?? column?.id ?? null
}

function assetLabel(asset) {
  if (!asset) return 'Unknown table'
  return `${quoteIdentifier(asset.schema_name)}.${quoteIdentifier(asset.table_name)}`
}

function columnName(column) {
  return column?.column_name ?? column?.name ?? 'Unnamed column'
}

function nativeType(column) {
  return String(column?.native_type_name ?? column?.native_type ?? '').trim().toLowerCase()
}

function isSupportedScalar(column) {
  return Boolean(columnId(column))
    && String(column?.native_type_schema ?? '') === 'pg_catalog'
    && SUPPORTED_SCALAR_TYPES.has(nativeType(column))
}

function runId(run) {
  return run?.run_id ?? run?.id ?? null
}

function runState(run) {
  return String(run?.state ?? run?.status ?? '').toUpperCase()
}

function dateText(value, fallback = 'Not recorded') {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString()
}

function ageText(value) {
  if (!value) return 'Not completed'
  const completed = new Date(value).getTime()
  if (Number.isNaN(completed)) return 'Time unavailable'
  const elapsed = Math.max(0, Date.now() - completed)
  const minutes = Math.floor(elapsed / 60_000)
  if (minutes < 1) return 'Less than a minute'
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'}`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours} hour${hours === 1 ? '' : 's'}`
  const days = Math.floor(hours / 24)
  return `${days} day${days === 1 ? '' : 's'}`
}

function integerText(value) {
  const number = Number(value)
  return Number.isFinite(number) ? Math.trunc(number).toLocaleString() : '—'
}

function numberText(value) {
  // API numeric aggregates are serialized as decimal strings. Do not coerce
  // those values through JavaScript's IEEE-754 Number: a valid PostgreSQL
  // NUMERIC aggregate can exceed its exact integer range, and rounding it in
  // the presentation layer would misstate the persisted observation.
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '—'
  if (typeof value !== 'string') return '—'
  const decimal = value.trim()
  return /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(decimal) ? decimal : '—'
}

function percentText(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  const percent = number <= 1 ? number * 100 : number
  return `${percent.toLocaleString(undefined, { maximumFractionDigits: 2 })}%`
}

function samplePercentText(value) {
  // The persisted sampling configuration is already expressed in percentage
  // points (for example, 1 means one percent), unlike a null rate which is a
  // fractional ratio. Never turn a 1% profile target into an apparent 100%.
  const number = Number(value)
  return Number.isFinite(number)
    ? `${number.toLocaleString(undefined, { maximumFractionDigits: 4 })}%`
    : '—'
}

function runStateLabel(state) {
  if (state === 'QUEUED') return 'Queued'
  if (state === 'RUNNING') return 'Running'
  if (state === 'SUCCEEDED') return 'Completed'
  if (state === 'FAILED') return 'Failed'
  if (state === 'CANCELLED') return 'Cancelled'
  return 'Status unavailable'
}

function runStateClass(state) {
  if (state === 'QUEUED' || state === 'RUNNING') return 'source-profile-state-active'
  if (state === 'SUCCEEDED') return 'source-profile-state-success'
  if (state === 'FAILED') return 'source-profile-state-failed'
  if (state === 'CANCELLED') return 'source-profile-state-cancelled'
  return 'source-profile-state-unknown'
}

function runMessage(run, cancellationPending) {
  const state = runState(run)
  if (cancellationPending && !TERMINAL_RUN_STATES.has(state)) {
    return 'Cancellation requested. Waiting for the worker to confirm the final status.'
  }
  if (state === 'QUEUED') return 'A bounded profile is queued. This does not confirm that a worker is currently reading the database.'
  if (state === 'RUNNING') return 'A worker is collecting aggregate observations from the selected bounded sample.'
  if (state === 'SUCCEEDED') return 'This bounded profile completed. Results describe only its observed sample.'
  if (state === 'FAILED') return 'This profile did not complete. No partial profile is presented as a completed result.'
  if (state === 'CANCELLED') return 'This profile was cancelled. No further results from that active run can be published.'
  return 'No database profiling run has been requested yet.'
}

function itemsFrom(response, keys) {
  if (Array.isArray(response)) return response
  for (const key of keys) {
    if (Array.isArray(response?.[key])) return response[key]
  }
  return []
}

function profileRunFrom(response) {
  return response?.run ?? response?.profile_run ?? response
}

function profileRunsFrom(response) {
  return itemsFrom(response, ['items', 'runs', 'profile_runs'])
}

function observationGroups(response) {
  return {
    tables: itemsFrom(response, ['table_observations', 'tables']),
    columns: itemsFrom(response, ['column_observations', 'columns']),
  }
}

function samplingMethodText(method) {
  if (String(method || '').toUpperCase() === 'SYSTEM_TABLE_SAMPLE') return 'PostgreSQL SYSTEM table sample'
  return method ? String(method) : 'Sampling method unavailable'
}

function populationText(population) {
  if (String(population || '').toUpperCase() === 'OBSERVED_SAMPLE') return 'Observed sample only'
  return population ? String(population) : 'Observed population unavailable'
}

function warningText(codes) {
  if (!Array.isArray(codes) || codes.length === 0) return null
  return codes.map(code => String(code).replace(/_/g, ' ')).join(' · ')
}

function resultAssetLabel(observation, currentAssetsById, tableObservationsById = new Map()) {
  const inlineAsset = observation?.asset
  if (inlineAsset?.schema_name || inlineAsset?.table_name) return assetLabel(inlineAsset)
  if (observation?.schema_name || observation?.table_name) return assetLabel(observation)
  const tableObservation = tableObservationsById.get(String(observation?.table_observation_id ?? ''))
  if (tableObservation) return resultAssetLabel(tableObservation, currentAssetsById)
  const known = currentAssetsById.get(String(observation?.asset_id ?? ''))
  if (known) return assetLabel(known)
  return 'Authorized table'
}

function selectedColumnsForDetail(detail) {
  const columns = detail?.columns ?? detail?.asset?.columns ?? []
  return Array.isArray(columns) ? columns.filter(isSupportedScalar) : []
}

function initialSelectionForDetail(detail) {
  return selectedColumnsForDetail(detail)
    .slice(0, CLIENT_MAX_COLUMNS_PER_TABLE)
    .map(column => String(columnId(column)))
}

function SelectedProfileScope({ selectedEntries, onToggleTable }) {
  if (selectedEntries.length === 0) return null
  return (
    <section className="source-profile-selected-scope" aria-labelledby="source-profile-selected-scope-heading">
      <h6 id="source-profile-selected-scope-heading">Selected profile scope</h6>
      <ul>
        {selectedEntries.map(selection => (
          <li key={selection.assetId}>
            <span>{assetLabel(selection.asset)} · {selection.columnIds.length} selected scalar column{selection.columnIds.length === 1 ? '' : 's'}</span>
            <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => onToggleTable(selection.asset, false)}>Remove</button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ScopeTableSelector({
  snapshotId,
  scopeMatchesCurrentSelection,
  assets,
  selectedTables,
  selectedEntries,
  assetDetails,
  loadingAssetIds,
  errorsByAssetId,
  onToggleTable,
  onToggleColumn,
  onRetryDetail,
}) {
  if (!snapshotId) {
    return <><p className="source-profile-guidance">Publish metadata before choosing a table profile scope.</p><SelectedProfileScope selectedEntries={selectedEntries} onToggleTable={onToggleTable} /></>
  }
  if (!scopeMatchesCurrentSelection) {
    return <><p className="source-profile-guidance" role="note">The displayed metadata is filtered by a changed selected-table scope. Refresh metadata before requesting a profile.</p><SelectedProfileScope selectedEntries={selectedEntries} onToggleTable={onToggleTable} /></>
  }
  if (!assets.length) {
    return <><p className="source-profile-guidance">No current authorized tables are loaded on this metadata page. Browse or refresh metadata before selecting a bounded profile scope.</p><SelectedProfileScope selectedEntries={selectedEntries} onToggleTable={onToggleTable} /></>
  }

  return (
    <div className="source-profile-selector" aria-label="Bounded database profile scope">
      <p className="source-profile-guidance">Choose only current authorized catalog tables. Selecting a table preselects up to {CLIENT_MAX_COLUMNS_PER_TABLE} supported scalar columns; deselect columns to narrow the profile. This browser also limits selection to {CLIENT_MAX_TABLES_PER_RUN} tables, while the server remains authoritative for table, column, time, and operation budgets.</p>
      <div className="source-profile-table-list">
        {assets.map(asset => {
          const id = assetId(asset)
          if (!id) return null
          const selected = selectedTables[id]
          const detail = assetDetails[id]
          const loading = loadingAssetIds.has(id)
          const error = errorsByAssetId[id]
          const supportedColumns = selectedColumnsForDetail(detail)
          const selectedColumnIds = selected?.columnIds ?? []
          return (
            <article className="source-profile-table" key={id}>
              <label className="source-profile-table-toggle">
                <input
                  type="checkbox"
                  checked={Boolean(selected)}
                  onChange={event => onToggleTable(asset, event.target.checked)}
                  disabled={!selected && selectedEntries.length >= CLIENT_MAX_TABLES_PER_RUN}
                />
                <span>{assetLabel(asset)}</span>
              </label>
              {selected && (
                <div className="source-profile-columns">
                  {loading && <p className="source-profile-mini-status" role="status">Loading current column identities…</p>}
                  {error && (
                    <div className="source-profile-inline-error" role="alert">
                      <span>{error}</span>
                      <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => onRetryDetail(asset)}>Retry metadata</button>
                    </div>
                  )}
                  {!loading && !error && detail && (
                    <>
                      {supportedColumns.length ? (
                        <fieldset>
                          <legend>Supported scalar columns ({selectedColumnIds.length}/{supportedColumns.length})</legend>
                          <div className="source-profile-column-list">
                            {supportedColumns.map(column => {
                              const id = String(columnId(column))
                              return (
                                <label key={id} className="source-profile-column-toggle">
                                  <input
                                    type="checkbox"
                                    checked={selectedColumnIds.includes(id)}
                                    onChange={event => onToggleColumn(assetId(asset), id, event.target.checked)}
                                    disabled={!selectedColumnIds.includes(id) && selectedColumnIds.length >= CLIENT_MAX_COLUMNS_PER_TABLE}
                                  />
                                  <span>{quoteIdentifier(columnName(column))}</span>
                                  <small>{column.formatted_native_type || nativeType(column)}</small>
                                </label>
                              )
                            })}
                          </div>
                        </fieldset>
                      ) : (
                        <p className="source-profile-mini-status">No supported scalar columns are available. You can still request the bounded observed-row count for this table.</p>
                      )}
                      {(detail?.columns?.length ?? 0) > supportedColumns.length && (
                        <p className="source-profile-mini-status">Unsupported types are intentionally omitted; this stage does not collect raw values or text extrema.</p>
                      )}
                    </>
                  )}
                </div>
              )}
            </article>
          )
        })}
      </div>
      <SelectedProfileScope selectedEntries={selectedEntries} onToggleTable={onToggleTable} />
    </div>
  )
}

function ProfileRunCard({ run, active, selected, cancellationPending, onInspect, onCancel }) {
  const state = runState(run)
  const id = runId(run)
  const canCancel = ACTIVE_RUN_STATES.has(state)
  const cancellationInFlight = cancellationPending && selected
  return (
    <article className={`source-profile-run ${selected ? 'selected' : ''}`}>
      <div className="source-profile-run-heading">
        <div>
          <h5>{runStateLabel(state)}</h5>
          <p>{dateText(run.completed_at || run.started_at || run.requested_at, 'Request time unavailable')}</p>
        </div>
        <span className={`source-profile-state ${runStateClass(state)}`}>{runStateLabel(state)}</span>
      </div>
      <dl className="source-profile-run-summary">
        <div><dt>Tables</dt><dd>{integerText(run.selected_asset_count)}</dd></div>
        <div><dt>Columns</dt><dd>{integerText(run.selected_column_count)}</dd></div>
        <div><dt>Sample limit</dt><dd>{integerText(run.sample_limit)}</dd></div>
        <div><dt>Operations</dt><dd>{integerText(run.max_operations)}</dd></div>
        <div><dt>Profile age</dt><dd>{ageText(run.completed_at)}</dd></div>
      </dl>
      {state === 'FAILED' && run.failure_code && <p className="source-profile-failure">Failure category: {String(run.failure_code).replace(/_/g, ' ')}</p>}
      <div className="source-actions source-profile-run-actions">
        <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => onInspect(run)} disabled={!id || active || cancellationInFlight}>
          {active ? 'Loading…' : cancellationInFlight ? 'Cancellation requested…' : state === 'SUCCEEDED' ? 'Inspect observations' : 'Inspect run'}
        </button>
        {canCancel && (
          <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => onCancel(run)} disabled={cancellationPending}>
            {cancellationInFlight ? 'Cancellation requested…' : 'Cancel profile'}
          </button>
        )}
      </div>
    </article>
  )
}

function ProfileObservations({ observations, currentAssetsById }) {
  const { tables, columns } = observationGroups(observations)
  const tableObservationsById = new Map(
    tables.map(observation => [String(observation.table_observation_id ?? observation.observation_id ?? ''), observation]),
  )
  if (!tables.length && !columns.length) {
    return <p className="source-profile-guidance">No completed aggregate observations are available for this run.</p>
  }
  return (
    <div className="source-profile-results">
      <section className="source-profile-result-section" aria-labelledby="source-profile-table-observations-heading">
        <h5 id="source-profile-table-observations-heading">Observed table samples</h5>
        {!tables.length ? <p className="source-profile-guidance">No table observations were published.</p> : (
          <div className="source-profile-observation-list">
            {tables.map(observation => (
              <article className="source-profile-observation" key={observation.table_observation_id ?? observation.observation_id ?? observation.asset_id}>
                <div className="source-profile-observation-heading">
                  <h6>{resultAssetLabel(observation, currentAssetsById)}</h6>
                  <span>{populationText(observation.population)}</span>
                </div>
                <dl className="source-profile-observation-summary">
                  <div><dt>Observed rows</dt><dd>{integerText(observation.observed_row_count)}</dd></div>
                  <div><dt>Status</dt><dd>{String(observation.status || 'unavailable').replace(/_/g, ' ')}</dd></div>
                  <div><dt>Sample</dt><dd>{samplePercentText(observation.sample_percent)} · limit {integerText(observation.sample_limit)}</dd></div>
                  <div><dt>Method</dt><dd>{samplingMethodText(observation.sampling_method)}</dd></div>
                  <div><dt>Repeatable seed</dt><dd>{integerText(observation.sample_seed)}</dd></div>
                  <div><dt>Collected</dt><dd>{dateText(observation.collected_at, 'Time unavailable')}</dd></div>
                </dl>
                {warningText(observation.warning_codes) && <p className="source-profile-warning">Limitations: {warningText(observation.warning_codes)}</p>}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="source-profile-result-section" aria-labelledby="source-profile-column-observations-heading">
        <h5 id="source-profile-column-observations-heading">Observed column aggregates</h5>
        <p className="source-profile-guidance">Counts, rates, distinct counts, and numeric summaries below are exact only for the indicated observed sample. They do not establish full-table uniqueness or absence of nulls.</p>
        {!columns.length ? <p className="source-profile-guidance">No supported column observations were published.</p> : (
          <div className="source-profile-observation-list">
            {columns.map(observation => {
              const hasNumeric = observation.numeric_count != null
                || observation.numeric_sum != null
                || observation.numeric_minimum != null
                || observation.numeric_maximum != null
                || observation.numeric_mean != null
              return (
                <article className="source-profile-observation" key={observation.column_observation_id ?? observation.observation_id ?? observation.column_id}>
                  <div className="source-profile-observation-heading">
                    <h6>{resultAssetLabel(observation, currentAssetsById, tableObservationsById)} · {quoteIdentifier(observation.column_name || 'Column')}</h6>
                    <span>{populationText(observation.population)}</span>
                  </div>
                  <dl className="source-profile-observation-summary">
                    <div><dt>Observed rows</dt><dd>{integerText(observation.observed_row_count)}</dd></div>
                    <div><dt>Nulls</dt><dd>{integerText(observation.null_count)} · {percentText(observation.null_rate)}</dd></div>
                    <div><dt>Non-null</dt><dd>{integerText(observation.non_null_count)}</dd></div>
                    <div><dt>Distinct (sample)</dt><dd>{integerText(observation.distinct_count)}</dd></div>
                    <div><dt>Status</dt><dd>{String(observation.status || 'unavailable').replace(/_/g, ' ')}</dd></div>
                  </dl>
                  {hasNumeric && (
                    <dl className="source-profile-observation-summary source-profile-numeric-summary">
                      <div><dt>Numeric values</dt><dd>{integerText(observation.numeric_count)}</dd></div>
                      <div><dt>Sum</dt><dd>{numberText(observation.numeric_sum)}</dd></div>
                      <div><dt>Minimum</dt><dd>{numberText(observation.numeric_minimum)}</dd></div>
                      <div><dt>Maximum</dt><dd>{numberText(observation.numeric_maximum)}</dd></div>
                      <div><dt>Mean</dt><dd>{numberText(observation.numeric_mean)}</dd></div>
                    </dl>
                  )}
                  {warningText(observation.warning_codes) && <p className="source-profile-warning">Limitations: {warningText(observation.warning_codes)}</p>}
                </article>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}

function ProfileFindings({ findings }) {
  const items = itemsFrom(findings, ['items', 'findings'])
  if (!items.length) return <p className="source-profile-guidance">No deterministic findings met the sample-qualified thresholds for this run.</p>
  return (
    <div className="source-profile-finding-list">
      {items.map(finding => (
        <article className="source-profile-finding" key={finding.finding_id ?? finding.id ?? `${finding.finding_type}-${finding.column_observation_id}`}>
          <div className="source-profile-finding-heading">
            <h6>{finding.title || 'Sample-qualified finding'}</h6>
            <span>{String(finding.severity || 'info').toLowerCase()}</span>
          </div>
          <p>{finding.description || 'This deterministic finding is supported by the observed aggregate profile.'}</p>
          <p className="source-profile-finding-limitations">Limitations: {finding.limitations || 'This result applies only to the observed sample.'}</p>
          {finding.column_name && <p className="source-profile-finding-provenance">Supporting column: {quoteIdentifier(finding.column_name)}</p>}
        </article>
      ))}
    </div>
  )
}

function ProfileCharts({ run, observations }) {
  const chartSpecs = buildProfileNullRateChartSpecs({ run, observations })
  const eligibleSpecs = chartSpecs.filter(spec => spec.eligible)
  const unavailableMessages = [...new Set(chartSpecs.filter(spec => !spec.eligible).map(spec => spec.message))]
  if (!eligibleSpecs.length) {
    return (
      <section className="source-profile-result-section" aria-labelledby="source-profile-charts-heading">
        <h5 id="source-profile-charts-heading">Observed null-rate charts</h5>
        <p className="source-profile-guidance">{unavailableMessages[0] || 'No compatible observed null-rate chart is available for this profile.'}</p>
      </section>
    )
  }
  return (
    <section className="source-profile-result-section" aria-labelledby="source-profile-charts-heading">
      <h5 id="source-profile-charts-heading">Observed null-rate charts</h5>
      <p className="source-profile-guidance">Each chart uses only one retained observed table sample. Exact observed rates and supporting counts remain in the aggregate observations above.</p>
      <div style={{ display: 'grid', gap: 12 }}>
        {eligibleSpecs.map(spec => (
          <DatabaseChartRenderer key={spec.provenance.table_observation_id} spec={spec} />
        ))}
      </div>
      {unavailableMessages.map(message => <p key={message} className="source-profile-guidance">{message}</p>)}
    </section>
  )
}

export default function SourceProfilingPanel({
  source,
  sourceId,
  snapshotId,
  assets = [],
  selectedAssetDetail,
  scopeMatchesCurrentSelection = true,
  onProfilingChanged,
}) {
  const sourceProfilingEnabled = Boolean(source?.profiling_enabled)
  const [open, setOpen] = useState(false)
  const [profilingEnabled, setProfilingEnabled] = useState(sourceProfilingEnabled)
  const [enableAcknowledged, setEnableAcknowledged] = useState(false)
  const [configUpdating, setConfigUpdating] = useState(false)
  const [runs, setRuns] = useState([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [runsError, setRunsError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [selectedRun, setSelectedRun] = useState(null)
  const [cancellationPending, setCancellationPending] = useState(false)
  const [selectedTables, setSelectedTables] = useState({})
  const [assetDetails, setAssetDetails] = useState({})
  const [loadingAssetIds, setLoadingAssetIds] = useState(() => new Set())
  const [errorsByAssetId, setErrorsByAssetId] = useState({})
  const [requestingRun, setRequestingRun] = useState(false)
  const [observations, setObservations] = useState(null)
  const [findings, setFindings] = useState(null)
  const [resultsLoading, setResultsLoading] = useState(false)
  const [resultsError, setResultsError] = useState(null)

  const mountedRef = useRef(false)
  const requestVersionRef = useRef(0)
  const listControllerRef = useRef(null)
  const mutationControllerRef = useRef(null)
  const resultControllerRef = useRef(null)
  const detailControllersRef = useRef(new Map())
  const priorSourceIdRef = useRef(sourceId)

  const currentAssets = useMemo(() => {
    const byId = new Map()
    for (const asset of assets) {
      const id = assetId(asset)
      if (id) byId.set(String(id), asset)
    }
    return byId
  }, [assets])
  const currentAssetsList = useMemo(() => [...currentAssets.values()], [currentAssets])
  const selectedEntries = useMemo(() => Object.values(selectedTables), [selectedTables])
  const hasActiveRun = runs.some(run => ACTIVE_RUN_STATES.has(runState(run)))
  const selectedRunState = runState(selectedRun)
  const shouldPoll = Boolean(selectedRun && ACTIVE_RUN_STATES.has(selectedRunState))
  const hasUnresolvedSelection = selectedEntries.some(selection => (
    loadingAssetIds.has(String(selection.assetId)) || Boolean(errorsByAssetId[selection.assetId]) || !assetDetails[selection.assetId]
  ))
  const canRequestRun = profilingEnabled
    && Boolean(snapshotId)
    && scopeMatchesCurrentSelection
    && selectedEntries.length > 0
    && !hasUnresolvedSelection
    && !requestingRun
    && !hasActiveRun

  const clearResults = useCallback(() => {
    resultControllerRef.current?.abort()
    setObservations(null)
    setFindings(null)
    setResultsError(null)
    setResultsLoading(false)
  }, [])

  const loadRuns = useCallback(async () => {
    listControllerRef.current?.abort()
    const controller = new AbortController()
    const requestVersion = requestVersionRef.current + 1
    requestVersionRef.current = requestVersion
    listControllerRef.current = controller
    setRunsLoading(true)
    try {
      const response = await sourceApi.listProfileRuns(sourceId, { page: 1, pageSize: RUN_PAGE_SIZE }, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current || requestVersion !== requestVersionRef.current) return []
      const nextRuns = profileRunsFrom(response)
      setRuns(nextRuns)
      setRunsError(null)
      setSelectedRun(current => {
        const currentId = runId(current)
        if (currentId) return nextRuns.find(run => String(runId(run)) === String(currentId)) ?? current
        return nextRuns[0] ?? null
      })
      return nextRuns
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current && requestVersion === requestVersionRef.current) {
        setRunsError(safeErrorMessage(error, 'We could not load database profile runs.'))
      }
      return []
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestVersion === requestVersionRef.current) setRunsLoading(false)
    }
  }, [sourceId])

  const loadResults = useCallback(async run => {
    const id = runId(run)
    if (!id) return
    resultControllerRef.current?.abort()
    const controller = new AbortController()
    resultControllerRef.current = controller
    setResultsLoading(true)
    setResultsError(null)
    try {
      const [nextObservations, nextFindings] = await Promise.all([
        sourceApi.getProfileObservations(sourceId, id, { page: 1, pageSize: RESULT_PAGE_SIZE }, { signal: controller.signal }),
        sourceApi.getProfileFindings(sourceId, id, { page: 1, pageSize: RESULT_PAGE_SIZE }, { signal: controller.signal }),
      ])
      if (controller.signal.aborted || !mountedRef.current) return
      setObservations(nextObservations)
      setFindings(nextFindings)
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        setResultsError(safeErrorMessage(error, 'We could not load the aggregate observations and findings for this profile run.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setResultsLoading(false)
    }
  }, [sourceId])

  useEffect(() => {
    mountedRef.current = true
    const detailControllers = detailControllersRef.current
    return () => {
      mountedRef.current = false
      requestVersionRef.current += 1
      listControllerRef.current?.abort()
      mutationControllerRef.current?.abort()
      resultControllerRef.current?.abort()
      for (const controller of detailControllers.values()) controller.abort()
      detailControllers.clear()
    }
  }, [sourceId])

  useEffect(() => {
    // A parent update after this panel's own PUT should not overwrite the
    // confirmed local configuration with an older list item. A genuinely
    // different source resets all source-scoped browser state.
    if (priorSourceIdRef.current === sourceId) return
    priorSourceIdRef.current = sourceId
    setProfilingEnabled(sourceProfilingEnabled)
    setEnableAcknowledged(false)
    setRuns([])
    setRunsError(null)
    setNotice(null)
    setSelectedRun(null)
    setCancellationPending(false)
    setSelectedTables({})
    setAssetDetails({})
    setLoadingAssetIds(new Set())
    setErrorsByAssetId({})
    clearResults()
  }, [clearResults, sourceId, sourceProfilingEnabled])

  useEffect(() => {
    // Snapshot-scoped IDs must never carry into a subsequent catalog head.
    setSelectedTables({})
    setAssetDetails({})
    setLoadingAssetIds(new Set())
    setErrorsByAssetId({})
  }, [snapshotId])

  useEffect(() => {
    if (open) void loadRuns()
  }, [loadRuns, open])

  useEffect(() => {
    if (!shouldPoll) return undefined
    let cancelled = false
    let timer
    let controller
    let attempts = 0
    const id = runId(selectedRun)

    const poll = async () => {
      if (cancelled || !id || attempts >= MAX_POLL_ATTEMPTS) return
      controller = new AbortController()
      try {
        const response = await sourceApi.getProfileRun(sourceId, id, { signal: controller.signal })
        const next = profileRunFrom(response)
        if (cancelled || !mountedRef.current) return
        setSelectedRun(next)
        setRuns(current => {
          const nextId = String(runId(next))
          const replaced = current.some(run => String(runId(run)) === nextId)
            ? current.map(run => String(runId(run)) === nextId ? next : run)
            : [next, ...current]
          return replaced
        })
        const nextState = runState(next)
        if (TERMINAL_RUN_STATES.has(nextState)) {
          setCancellationPending(false)
          if (nextState === 'SUCCEEDED') {
            setNotice('Database profiling completed. The displayed results are aggregate-only observations from its bounded sample.')
            await loadResults(next)
          } else if (nextState === 'FAILED') {
            setNotice('Database profiling failed before a completed profile could be published.')
          } else {
            setNotice('Database profiling was cancelled. No further results from that active run were published.')
          }
          void loadRuns()
          return
        }
      } catch (error) {
        const code = sourceErrorCode(error)
        if (!cancelled && (error?.status === 401 || error?.status === 403 || error?.status === 404
          || code === 'authentication_required' || code === 'source_not_found')) {
          setCancellationPending(false)
          setRunsError(safeErrorMessage(error, 'This saved connection is no longer available.'))
          return
        }
        if (!cancelled && !controller.signal.aborted) {
          setNotice('Unable to confirm profile status just now. The profile may still be active; no duplicate request is being offered.')
        }
      }
      attempts += 1
      if (!cancelled && attempts < MAX_POLL_ATTEMPTS) {
        const delay = Math.min(MAX_POLL_DELAY_MS, INITIAL_POLL_DELAY_MS * (2 ** Math.min(attempts, 4)))
        timer = window.setTimeout(poll, delay)
      } else if (!cancelled) {
        setNotice('The profile is still active, but automatic status polling paused after repeated checks. Reopen this panel later to reconcile its status.')
      }
    }
    timer = window.setTimeout(poll, INITIAL_POLL_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      controller?.abort()
    }
  }, [loadResults, loadRuns, selectedRun, shouldPoll, sourceId])

  const profileDetailFromProps = useCallback(asset => {
    const selected = selectedAssetDetail?.asset ?? selectedAssetDetail
    const selectedId = assetId(selected)
    if (String(selectedId ?? '') !== String(assetId(asset) ?? '')) return null
    if (selectedAssetDetail?.snapshot_id && String(selectedAssetDetail.snapshot_id) !== String(snapshotId)) return null
    if (selectedAssetDetail?.scope_matches_current_selection === false) return null
    return selectedAssetDetail
  }, [selectedAssetDetail, snapshotId])

  const storeDetail = useCallback((asset, detail) => {
    const id = String(assetId(asset))
    setAssetDetails(current => ({ ...current, [id]: detail }))
    setSelectedTables(current => {
      if (!current[id]) return current
      return {
        ...current,
        [id]: {
          ...current[id],
          columnIds: initialSelectionForDetail(detail),
        },
      }
    })
    setErrorsByAssetId(current => {
      if (!current[id]) return current
      const next = { ...current }
      delete next[id]
      return next
    })
  }, [])

  const loadAssetDetail = useCallback(async asset => {
    const id = String(assetId(asset) ?? '')
    if (!id || !snapshotId) return
    const fromProps = profileDetailFromProps(asset)
    if (fromProps) {
      storeDetail(asset, fromProps)
      return
    }
    detailControllersRef.current.get(id)?.abort()
    const controller = new AbortController()
    detailControllersRef.current.set(id, controller)
    setLoadingAssetIds(current => new Set([...current, id]))
    setErrorsByAssetId(current => {
      const next = { ...current }
      delete next[id]
      return next
    })
    try {
      const detail = await sourceApi.getCatalogAsset(sourceId, id, { snapshotId }, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      if (String(detail?.snapshot_id ?? detail?.asset?.snapshot_id ?? '') !== String(snapshotId)
        || detail?.scope_matches_current_selection === false) {
        setErrorsByAssetId(current => ({ ...current, [id]: 'Metadata changed while profile columns were loading. Refresh metadata and select this table again.' }))
        return
      }
      storeDetail(asset, detail)
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        setErrorsByAssetId(current => ({
          ...current,
          [id]: safeErrorMessage(error, 'We could not load current catalog columns for this selected table.'),
        }))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) {
        setLoadingAssetIds(current => {
          const next = new Set(current)
          next.delete(id)
          return next
        })
      }
    }
  }, [profileDetailFromProps, snapshotId, sourceId, storeDetail])

  const toggleTable = useCallback((asset, checked) => {
    const id = String(assetId(asset) ?? '')
    if (!id) return
    if (!checked) {
      detailControllersRef.current.get(id)?.abort()
      setSelectedTables(current => {
        const next = { ...current }
        delete next[id]
        return next
      })
      setLoadingAssetIds(current => {
        const next = new Set(current)
        next.delete(id)
        return next
      })
      setErrorsByAssetId(current => {
        const next = { ...current }
        delete next[id]
        return next
      })
      return
    }
    if (!snapshotId || !scopeMatchesCurrentSelection) return
    setSelectedTables(current => ({
      ...current,
      [id]: { assetId: id, asset, columnIds: [] },
    }))
    const cached = assetDetails[id]
    if (cached) storeDetail(asset, cached)
    else void loadAssetDetail(asset)
  }, [assetDetails, loadAssetDetail, scopeMatchesCurrentSelection, snapshotId, storeDetail])

  const toggleColumn = useCallback((assetIdValue, columnIdValue, checked) => {
    const assetKey = String(assetIdValue)
    const columnKey = String(columnIdValue)
    setSelectedTables(current => {
      const table = current[assetKey]
      if (!table) return current
      const columnIds = checked
        ? [...new Set([...table.columnIds, columnKey])]
        : table.columnIds.filter(id => id !== columnKey)
      return { ...current, [assetKey]: { ...table, columnIds } }
    })
  }, [])

  const updateProfiling = async enabled => {
    if (configUpdating || (enabled && !enableAcknowledged)) return
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setConfigUpdating(true)
    setRunsError(null)
    setNotice(null)
    try {
      const response = await sourceApi.configureProfiling(sourceId, { profiling_enabled: enabled }, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      setProfilingEnabled(response?.profiling_enabled ?? enabled)
      onProfilingChanged?.(response)
      setEnableAcknowledged(false)
      if (enabled) {
        setNotice('Optional database profiling is enabled. It will not run until you choose a current catalog scope and request a bounded profile.')
      } else {
        setSelectedTables({})
        clearResults()
        setNotice('Optional database profiling is disabled. New requests are blocked and active work is cancelled before it can publish further results. Completed aggregate-only historical profiles remain available when they still match your current access and scope.')
      }
      void loadRuns()
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        setRunsError(safeErrorMessage(error, enabled
          ? 'We could not enable optional database profiling. No profile was requested.'
          : 'We could not disable optional database profiling. The current setting has not been assumed to change.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setConfigUpdating(false)
    }
  }

  const requestProfile = async () => {
    if (!canRequestRun) return
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setRequestingRun(true)
    setRunsError(null)
    setNotice(null)
    const body = {
      tables: selectedEntries.map(selection => ({
        asset_id: selection.assetId,
        column_ids: selection.columnIds,
      })),
    }
    try {
      const response = await sourceApi.requestProfileRun(sourceId, body, { signal: controller.signal })
      const run = profileRunFrom(response)
      if (controller.signal.aborted || !mountedRef.current || !runId(run)) return
      setSelectedRun(run)
      setRuns(current => [run, ...current.filter(item => String(runId(item)) !== String(runId(run)))])
      setCancellationPending(false)
      clearResults()
      setNotice(runState(run) === 'QUEUED'
        ? 'Bounded database profile requested. It is queued until a worker claims it.'
        : 'Bounded database profile request accepted.')
      void loadRuns()
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        if (isUncertain(error)) {
          setNotice('We could not confirm whether the profile request was accepted. Checking existing profile runs before another request is offered.')
          await loadRuns()
        } else {
          setRunsError(safeErrorMessage(error, 'We could not request a bounded database profile.'))
        }
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setRequestingRun(false)
    }
  }

  const inspectRun = async run => {
    const id = runId(run)
    if (!id) return
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setRunsError(null)
    clearResults()
    try {
      const response = await sourceApi.getProfileRun(sourceId, id, { signal: controller.signal })
      const next = profileRunFrom(response)
      if (controller.signal.aborted || !mountedRef.current) return
      setSelectedRun(next)
      setRuns(current => current.map(item => String(runId(item)) === String(id) ? next : item))
      if (runState(next) === 'SUCCEEDED') await loadResults(next)
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) setRunsError(safeErrorMessage(error, 'We could not load that database profile run.'))
    }
  }

  const cancelRun = async run => {
    const id = runId(run)
    if (!id || cancellationPending || !ACTIVE_RUN_STATES.has(runState(run))) return
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setSelectedRun(run)
    setCancellationPending(true)
    setNotice('Cancellation requested. Waiting for the worker to confirm the final status.')
    try {
      const response = await sourceApi.cancelProfileRun(sourceId, id, { signal: controller.signal })
      const next = profileRunFrom(response)
      if (controller.signal.aborted || !mountedRef.current) return
      if (runId(next)) {
        setSelectedRun(next)
        setRuns(current => current.map(item => String(runId(item)) === String(id) ? next : item))
        if (TERMINAL_RUN_STATES.has(runState(next))) setCancellationPending(false)
      }
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        setNotice(isUncertain(error)
          ? 'We could not confirm cancellation yet. The current profile status will continue to be checked.'
          : safeErrorMessage(error, 'We could not request profile cancellation. The current status will be checked again.'))
      }
    }
  }

  return (
    <section className="source-profiling" aria-labelledby="source-profiling-heading">
      <div className="source-profiling-heading">
        <div>
          <h4 id="source-profiling-heading">Optional database profiling</h4>
          <p>Manual, bounded PostgreSQL reads that retain aggregate observations only. It does not create business definitions; eligible retained null-rate observations can be viewed as deterministic charts.</p>
        </div>
        <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => setOpen(current => !current)}>
          {open ? 'Close profiling' : 'Open profiling'}
        </button>
      </div>

      {!open ? (
        <p className="source-profile-guidance">Profiling is {profilingEnabled ? 'enabled but idle' : 'off by default'}. Opening this panel does not start a profile.</p>
      ) : (
        <>
          {runsError && <p className="source-alert" role="alert">{runsError}</p>}
          {notice && <p className="source-notice source-profile-notice" role="status">{notice}</p>}

          <section className="source-profile-config" aria-labelledby="source-profile-config-heading">
            <div>
              <h5 id="source-profile-config-heading">Profiling opt-in</h5>
              <p>Profiling reads the selected database tables through a restricted connection. NeuroNova stores only aggregate observations, deterministic findings, and bounded run provenance—not sampled rows, example values, top text values, credentials, or SQL.</p>
            </div>
            {!profilingEnabled ? (
              <div className="source-profile-config-actions">
                <label className="source-profile-acknowledgement">
                  <input type="checkbox" checked={enableAcknowledged} onChange={event => setEnableAcknowledged(event.target.checked)} />
                  <span>I understand that manual profiling reads selected database data and stores aggregate-only observations.</span>
                </label>
                <button className="btn btn-primary" type="button" disabled={!enableAcknowledged || configUpdating} onClick={() => updateProfiling(true)}>
                  {configUpdating ? 'Enabling…' : 'Enable optional profiling'}
                </button>
              </div>
            ) : (
              <div className="source-profile-config-actions">
                <p className="source-profile-enabled">Profiling is enabled. It remains idle until you request a run below.</p>
                <button className="btn btn-ghost-neutral" type="button" disabled={configUpdating} onClick={() => updateProfiling(false)}>
                  {configUpdating ? 'Disabling…' : 'Disable profiling'}
                </button>
              </div>
            )}
          </section>

          {profilingEnabled && (
            <section className="source-profile-request" aria-labelledby="source-profile-request-heading">
              <div>
                <h5 id="source-profile-request-heading">Request a bounded profile</h5>
                <p>Each table uses one fixed internal PostgreSQL SYSTEM table sample, with a repeatable seed and row cap shared by all of that table’s measures. SYSTEM sampling is block-based, can be biased, and can yield an empty sample; a row limit alone is not treated as a cost guarantee.</p>
              </div>
              <ScopeTableSelector
                snapshotId={snapshotId}
                scopeMatchesCurrentSelection={scopeMatchesCurrentSelection}
                assets={currentAssetsList}
                selectedTables={selectedTables}
                selectedEntries={selectedEntries}
                assetDetails={assetDetails}
                loadingAssetIds={loadingAssetIds}
                errorsByAssetId={errorsByAssetId}
                onToggleTable={toggleTable}
                onToggleColumn={toggleColumn}
                onRetryDetail={loadAssetDetail}
              />
              <div className="source-actions source-profile-request-actions">
                <button className="btn btn-primary" type="button" disabled={!canRequestRun} onClick={requestProfile}>
                  {requestingRun ? 'Requesting profile…' : hasActiveRun ? 'Profile already active' : 'Request bounded profile'}
                </button>
                {!selectedEntries.length && <span className="source-profile-mini-status">Choose at least one current authorized table.</span>}
                {hasUnresolvedSelection && <span className="source-profile-mini-status">Resolve the selected table metadata before requesting a profile.</span>}
              </div>
            </section>
          )}

          <section className="source-profile-runs" aria-labelledby="source-profile-runs-heading">
            <div className="source-profile-runs-heading">
              <div>
                <h5 id="source-profile-runs-heading">Profile runs</h5>
                <p>Latest {RUN_PAGE_SIZE} owner-scoped profile runs. Historical completed profiles remain available after opt-out only when their catalog artifacts still match current source access and scope.</p>
              </div>
              <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => void loadRuns()} disabled={runsLoading}>
                {runsLoading ? 'Refreshing…' : 'Refresh runs'}
              </button>
            </div>
            {runsLoading && !runs.length ? <p className="source-profile-guidance" role="status">Loading profile runs…</p> : (
              runs.length ? (
                <div className="source-profile-run-list">
                  {runs.map(run => (
                    <ProfileRunCard
                      key={runId(run)}
                      run={run}
                      selected={String(runId(selectedRun) ?? '') === String(runId(run) ?? '')}
                      active={resultsLoading && String(runId(selectedRun) ?? '') === String(runId(run) ?? '')}
                      cancellationPending={cancellationPending}
                      onInspect={inspectRun}
                      onCancel={cancelRun}
                    />
                  ))}
                </div>
              ) : <p className="source-profile-guidance">No database profile has been requested for this source.</p>
            )}
            {selectedRun && <p className="source-profile-run-message" aria-live="polite">{runMessage(selectedRun, cancellationPending)}</p>}
          </section>

          {selectedRun && runState(selectedRun) === 'SUCCEEDED' && (
            <section className="source-profile-inspection" aria-labelledby="source-profile-inspection-heading">
              <div className="source-profile-inspection-heading">
                <div>
                  <h5 id="source-profile-inspection-heading">Aggregate observations and deterministic findings</h5>
                  <p>Completed {dateText(selectedRun.completed_at, 'at an unavailable time')} · {samplingMethodText(selectedRun.sampling_method)} · {samplePercentText(selectedRun.sample_percent)} sample target · maximum {integerText(selectedRun.sample_limit)} observed rows per selected table.</p>
                </div>
                <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => void loadResults(selectedRun)} disabled={resultsLoading}>
                  {resultsLoading ? 'Loading…' : 'Refresh results'}
                </button>
              </div>
              {resultsError && <p className="source-alert" role="alert">{resultsError}</p>}
              {resultsLoading && !observations && <p className="source-profile-guidance" role="status">Loading aggregate profile results…</p>}
              {!resultsLoading && !resultsError && observations && <ProfileObservations observations={observations} currentAssetsById={currentAssets} />}
              {!resultsLoading && !resultsError && observations && <ProfileCharts run={selectedRun} observations={observations} />}
              {!resultsLoading && !resultsError && findings && (
                <section className="source-profile-findings" aria-labelledby="source-profile-findings-heading">
                  <h5 id="source-profile-findings-heading">Sample-qualified findings</h5>
                  <ProfileFindings findings={findings} />
                </section>
              )}
            </section>
          )}
        </>
      )}
    </section>
  )
}
