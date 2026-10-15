import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import Layout from '../components/Layout'
import { sourceApi, sourceErrorCode } from '../api/sources'
import { DatabaseChartRenderer } from '../components/charts/DatabaseChartRenderer'
import { buildDatabaseResultChartSpec } from '../components/charts/databaseChartSpec'
import AnswerView from '../components/answers/AnswerView'
import { answerFromRecord } from '../components/answers/answerState'
import TurnProgress from '../components/answers/TurnProgress'
import { CancelledNote, ClarificationCard, FailedCard, InterruptedNote, UnsupportedCard } from '../components/answers/OutcomeCards'

const SOURCE_SESSION_STORAGE_PREFIX = 'neuronova:source-chat-session:v1:'

const SAFE_SOURCE_ERRORS = {
  authentication_required: 'Please sign in again to use this database conversation.',
  catalog_not_found: 'This source needs published metadata before a database conversation can start.',
  catalog_snapshot_not_found: 'The published metadata snapshot is no longer available. Refresh the source metadata and try again.',
  catalog_unavailable: 'Published metadata is currently unavailable for this source.',
  catalog_scope_changed: 'The selected table scope changed. Refresh the source metadata before starting a conversation.',
  source_not_found: 'This saved database source is no longer available.',
  source_unavailable: 'This saved database source is currently unavailable.',
  access_changed: 'Your access to this database source changed. Start a new conversation after access is restored.',
  access_revoked: 'Your access to this database source was revoked.',
  credential_unavailable: 'This database source needs administrator attention before it can be queried.',
  connector_not_available: 'Database query execution is not available right now.',
  unsupported_execution_configuration: 'This source configuration is not supported for controlled database queries.',
  schema_changed: 'The source schema changed while the query was being prepared. Refresh metadata and try again.',
  metric_definition_changed: 'The confirmed metric changed before this result could be used. Review it and try again.',
  relationship_definition_changed: 'The approved relationship changed before this result could be used. Review it and try again.',
  execution_timeout: 'The controlled database query timed out before returning a result.',
  execution_cancelled: 'The database query was cancelled.',
}

function sourceSessionStorageKey(sourceId) {
  return `${SOURCE_SESSION_STORAGE_PREFIX}${sourceId}`
}

function readSavedSessionId(sourceId) {
  try {
    return window.sessionStorage.getItem(sourceSessionStorageKey(sourceId))
  } catch {
    return null
  }
}

function saveSessionId(sourceId, sessionId) {
  try {
    window.sessionStorage.setItem(sourceSessionStorageKey(sourceId), sessionId)
  } catch {
    // The server-owned transcript remains the source of truth.
  }
}

function clearSavedSessionId(sourceId) {
  try {
    window.sessionStorage.removeItem(sourceSessionStorageKey(sourceId))
  } catch {
    // Browser storage is only a convenience and can safely fail.
  }
}

function safeErrorMessage(error, fallback = 'We could not start this database conversation.') {
  const code = sourceErrorCode(error) || error?.response?.error?.code || error?.code
  return SAFE_SOURCE_ERRORS[code] || fallback
}

// SPEC-10 W6.2: the backend wrote an actionable message for this event (every one is a constant), so it comes first.
function safeEventMessage(data, fallback) {
  const code = data?.code || data?.failure_code || data?.error_code
  if (typeof data?.message === 'string' && data.message.trim()) return data.message
  return SAFE_SOURCE_ERRORS[code] || fallback
}

const WRITE_REFUSAL = 'This assistant only reads data. It cannot change data or the database structure.'
const MAX_QUESTION_CHARS = 4000
const QUESTION_TOO_LONG = 'The question is too long. Please shorten it to under 4,000 characters.'
const GENERIC_FAILURE = 'The database conversation could not be completed.'

// SPEC-10 D5. A failure is pre-acceptance when no `start` event arrived: the server never took the question.
function noticeFor(error) {
  const code = error?.code
  const serverCode = error?.response?.error?.code
  if (error?.status === 401 || code === 'authentication_required') return { text: 'Your sign-in expired. Please sign in again.', restore: true }
  if (error?.status === 409 || serverCode === 'turn_in_progress') {
    return { text: 'An earlier question in this conversation is still being answered (maybe in another tab). Wait for it to finish, then send again.', restore: true, refresh: true }
  }
  if (error?.status === 429) {
    const seconds = Number(error?.response?.retry_after)
    const minutes = Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds / 60) : null
    return { text: `You've reached the question limit for now. ${minutes ? `Try again in about ${minutes} ${minutes === 1 ? 'minute' : 'minutes'}.` : 'Try again later.'}`, restore: true }
  }
  if (code === 'connect_timeout' || error?.transient || error?.name === 'TypeError' || error?.name === 'TimeoutError') {
    return { text: "The server didn't respond. Your question wasn't sent — it's back in the box.", restore: true, reconcile: true }
  }
  return { text: safeErrorMessage(error, GENERIC_FAILURE), restore: true, reconcile: true }
}

function isSourceSession(session, sourceId) {
  return Boolean(session?.session_id && String(session.source_id) === String(sourceId))
}

function resultFromMessage(message) {
  const direct = message.postgres_result ?? message.database_result ?? null
  if (direct) return direct
  return message.query_result?.execution_id ? message.query_result : null
}

function outcomeFromRow(message, answer) {
  if (answer?.outcome) return answer.outcome
  if (message.unsupported) return 'unsupported'
  if (message.degraded_reason === 'execution_cancelled') return 'cancelled'
  return 'answer'
}

function toDisplayMessage(message) {
  const role = String(message.role ?? '').toLowerCase()
  // SPEC-09: a model-written-SQL turn carries its SQL, assumptions and resolved presentation spec.
  const answer = answerFromRecord(message.nl_sql)
  return {
    id: message.message_id ?? `${message.created_at ?? Date.now()}-${role}`,
    role: role === 'user' ? 'user' : 'assistant',
    text: message.content ?? '',
    postgresResult: resultFromMessage(message),
    answer,
    outcome: role === 'user' ? null : outcomeFromRow(message, answer),
    errorCode: answer?.failureCode ?? message.degraded_reason ?? null,
    stage: null,
    streaming: false,
  }
}

function renderedTypedValue(cell) {
  if (cell == null || typeof cell !== 'object') return '—'
  if (cell.kind === 'null' || cell.value == null) return '—'
  if (cell.kind === 'boolean') return cell.value ? 'true' : 'false'
  // Stage 4B deliberately serializes integers, decimals and non-finite
  // numbers as strings. Do not parse them through JavaScript Number here.
  return typeof cell.value === 'string' ? cell.value : '—'
}

function ResultMeta({ label, value }) {
  if (!value) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      <span style={{ fontFamily: 'var(--font-body)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</span>
      <code style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-secondary)', overflowWrap: 'anywhere' }}>{String(value)}</code>
    </div>
  )
}

function MetricProvenance({ provenance }) {
  if (!provenance || typeof provenance !== 'object') return null
  const name = provenance.name ?? null
  const version = provenance.version_number ?? null
  const unit = provenance.unit_label ?? null
  const timeBasis = provenance.default_date_column_name ?? null
  const timeZone = provenance.default_time_zone ?? null
  const aggregation = provenance.aggregation ?? null
  const aggregateColumn = provenance.aggregation_column_name ?? null
  const fixedFilterCount = provenance.fixed_filter_count
  if (!name && version == null && !unit && !timeBasis && !timeZone && !aggregation) return null

  const aggregationLabel = {
    count_all: 'Count rows',
    count_column: 'Count non-null values',
    sum: 'Sum',
    average: 'Average',
    minimum: 'Minimum',
    maximum: 'Maximum',
  }[aggregation] ?? aggregation

  return (
    <section aria-label="Confirmed metric provenance" style={{ margin: 0, padding: '9px 12px', borderBottom: '1px solid #BFDBFE', background: '#EFF6FF' }}>
      <strong style={{ display: 'block', fontFamily: 'var(--font-heading)', fontSize: 11, color: '#1D4ED8' }}>Confirmed metric</strong>
      {(name || version != null) && <p style={{ margin: '3px 0 0', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)' }}>{name || 'Metric'}{version != null ? ` · version ${version}` : ''}</p>}
      {aggregationLabel && <p style={{ margin: '5px 0 0', fontFamily: 'var(--font-body)', fontSize: 12, lineHeight: 1.45, color: 'var(--color-text-secondary)' }}>{aggregationLabel}{aggregateColumn ? ` · ${aggregateColumn}` : ''}</p>}
      {(unit || timeBasis || timeZone) && <p style={{ margin: '5px 0 0', fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-secondary)' }}>
        {unit && `Unit: ${unit}`}{unit && (timeBasis || timeZone) ? ' · ' : ''}{timeBasis && `Time basis: ${timeBasis}`}{timeBasis && timeZone ? ' · ' : ''}{timeZone && `Time zone: ${timeZone}`}
      </p>}
      {Number.isInteger(fixedFilterCount) && <p style={{ margin: '5px 0 0', fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{fixedFilterCount === 1 ? '1 fixed filter applied' : `${fixedFilterCount} fixed filters applied`}</p>}
    </section>
  )
}

function RelationshipProvenance({ provenance, semantics }) {
  if (!provenance || typeof provenance !== 'object') return null
  const version = provenance.version_number ?? null
  const joinType = String(provenance.join_type ?? '').toLowerCase()
  if (!['inner', 'left'].includes(joinType) && version == null) return null

  const lookupFiltersInWhere = semantics?.lookup_filters_in_where === true
  const lookupFiltersInOn = semantics?.lookup_filters_in_on === true
  const joinLabel = joinType === 'left' ? 'Left join' : joinType === 'inner' ? 'Inner join' : 'Approved join'
  const behavior = joinType === 'left'
    ? (lookupFiltersInWhere
        ? 'A lookup filter was applied after the join, so unmatched base rows were excluded.'
        : lookupFiltersInOn
          ? 'Lookup filters were applied in the join condition, preserving unmatched base rows.'
          : 'Unmatched base rows are retained by the approved join.')
    : 'Only base rows with a matching lookup row are included.'

  return (
    <section aria-label="Approved relationship provenance" style={{ margin: 0, padding: '9px 12px', borderBottom: '1px solid #C4B5FD', background: '#F5F3FF' }}>
      <strong style={{ display: 'block', fontFamily: 'var(--font-heading)', fontSize: 11, color: '#6D28D9' }}>Approved relationship</strong>
      <p style={{ margin: '3px 0 0', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)' }}>{joinLabel}{version != null ? ` · version ${version}` : ''}</p>
      <p style={{ margin: '5px 0 0', fontFamily: 'var(--font-body)', fontSize: 12, lineHeight: 1.45, color: 'var(--color-text-secondary)' }}>{behavior}</p>
    </section>
  )
}

function PostgresResultTable({ result }) {
  const [presentation, setPresentation] = useState('table')
  const [chartFailed, setChartFailed] = useState(false)

  if (!result) return null

  const succeeded = result.status === 'succeeded'
  const rows = Array.isArray(result.rows) ? result.rows : []
  const columns = Array.isArray(result.columns) ? result.columns : []
  const warnings = Array.isArray(result.warnings) ? result.warnings : []
  const partial = succeeded && result.complete === false
  const ordering = Array.isArray(result.ordering) ? result.ordering : []
  const requestedTopN = succeeded
    && result.explicit_top_n === true
    && Number.isInteger(result.requested_limit)
    && result.requested_limit > 0
  const metricProvenance = result.metric_provenance ?? null
  const metricName = metricProvenance?.name ?? null
  const metricVersion = metricProvenance?.version_number ?? null
  const relationshipProvenance = result.relationship_provenance ?? null
  const relationshipJoinType = String(relationshipProvenance?.join_type ?? '').toLowerCase()
  const relationshipVersion = relationshipProvenance?.version_number ?? null
  const chartSpec = buildDatabaseResultChartSpec(result)
  const chartAvailable = succeeded && chartSpec.eligible && !chartFailed
  const resultPanelId = `database-result-${String(result.execution_id ?? 'result').replace(/[^A-Za-z0-9_-]/g, '') || 'result'}`
  const selectPresentation = event => {
    const next = event.currentTarget.dataset.presentation
    if (next === 'table' || next === 'chart') setPresentation(next)
  }
  const handlePresentationKeyDown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const next = event.key === 'ArrowLeft' || event.key === 'Home' ? 'table' : 'chart'
    setPresentation(next)
    event.currentTarget.parentElement?.querySelector(`[data-presentation="${next}"]`)?.focus()
  }

  return (
    <section aria-label="Controlled PostgreSQL result" style={{ marginTop: 10, border: '1px solid var(--color-border)', borderRadius: 8, overflow: 'hidden', background: 'white' }}>
      <div style={{ padding: '9px 11px', background: 'var(--color-base)', borderBottom: '1px solid var(--color-border)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
          <strong style={{ fontFamily: 'var(--font-heading)', fontSize: 12, color: 'var(--color-text-primary)' }}>Controlled database result</strong>
          <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: succeeded ? 'var(--color-success)' : 'var(--color-danger)' }}>{succeeded ? 'Read-only query' : 'No result disclosed'}</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8, marginTop: 8 }}>
          <ResultMeta label="Execution" value={result.execution_id} />
          <ResultMeta label="Source" value={result.source_id} />
          <ResultMeta label="Snapshot" value={result.snapshot_id} />
          <ResultMeta label="Metric" value={metricName ? `${metricName}${metricVersion != null ? ` · v${metricVersion}` : ''}` : null} />
          <ResultMeta label="Relationship" value={relationshipJoinType ? `${relationshipJoinType} join${relationshipVersion != null ? ` · v${relationshipVersion}` : ''}` : null} />
        </div>
      </div>

      {!succeeded ? (
        <p role="status" style={{ margin: 0, padding: '10px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: '#9F1239', background: '#FFF1F2' }}>
          {safeEventMessage(result, 'The controlled database query did not return a result.')}
        </p>
      ) : (
        <>
          {partial && (
            <p role="status" style={{ margin: 0, padding: '9px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: '#92400E', background: '#FFFBEB', borderBottom: '1px solid #FDE68A' }}>
              This result is partial because a safety row or byte limit was reached. It must not be treated as complete.
            </p>
          )}
          {requestedTopN && (
            <p role="status" style={{ margin: 0, padding: '9px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: '#1D4ED8', background: '#EFF6FF', borderBottom: '1px solid #BFDBFE' }}>
              This shows the requested top {result.requested_limit} {result.requested_limit === 1 ? 'result' : 'results'}{ordering.length > 0 ? ' using the displayed ordering.' : '.'} This question limit is not a safety truncation.
            </p>
          )}
          <MetricProvenance provenance={metricProvenance} />
          <RelationshipProvenance provenance={relationshipProvenance} semantics={result.calculation_semantics ?? {}} />
          {warnings.map((warning, index) => (
            <p key={`${warning}-${index}`} role="status" style={{ margin: 0, padding: '8px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: '#92400E', background: '#FFFBEB', borderBottom: '1px solid #FDE68A' }}>{warning}</p>
          ))}
          {rows.length > 0 && (
            <>
              {chartAvailable ? (
                <div role="tablist" aria-label="Database result presentation" style={{ display: 'flex', gap: 6, padding: '9px 11px', borderBottom: '1px solid var(--color-border-light)', background: 'var(--color-base)' }}>
                  <button type="button" role="tab" data-presentation="table" aria-selected={presentation === 'table'} aria-controls={`${resultPanelId}-table`} onClick={selectPresentation} onKeyDown={handlePresentationKeyDown} className="btn btn-ghost-neutral btn-sm">Table</button>
                  <button type="button" role="tab" data-presentation="chart" aria-selected={presentation === 'chart'} aria-controls={`${resultPanelId}-chart`} onClick={selectPresentation} onKeyDown={handlePresentationKeyDown} className="btn btn-ghost-neutral btn-sm">Chart</button>
                </div>
              ) : (
                <p role="note" style={{ margin: 0, padding: '9px 12px', borderBottom: '1px solid var(--color-border-light)', fontFamily: 'var(--font-body)', fontSize: 12, lineHeight: 1.45, color: 'var(--color-text-secondary)' }}>
                  {chartFailed ? 'The chart could not be rendered. The exact table result remains available and no query was rerun.' : chartSpec.message}
                </p>
              )}
              {chartAvailable && presentation === 'chart' && (
                <div id={`${resultPanelId}-chart`} role="tabpanel" aria-label="Database result chart" style={{ padding: 11 }}>
                  <DatabaseChartRenderer
                    spec={chartSpec}
                    onFailure={() => {
                      setChartFailed(true)
                      setPresentation('table')
                    }}
                  />
                </div>
              )}
            </>
          )}
          {rows.length === 0 ? (
            <p style={{ margin: 0, padding: '14px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
              {partial
                ? 'No result rows could be safely displayed before a safety limit was reached. This does not mean no rows matched.'
                : 'No rows matched this query.'}
            </p>
          ) : (presentation === 'table' || !chartAvailable) && (
            <div id={`${resultPanelId}-table`} role="tabpanel" aria-label="Database result table" style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ background: 'var(--color-base)' }}>
                    {columns.map((column, index) => (
                      <th key={`${column.alias}-${index}`} scope="col" style={{ padding: '7px 10px', fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textAlign: 'left', borderBottom: '1px solid var(--color-border)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                        <span>{column.alias ?? `column_${index + 1}`}</span>
                        {column.logical_type && <small style={{ display: 'block', marginTop: 2, fontFamily: 'var(--font-data)', fontSize: 9, fontWeight: 400, textTransform: 'none' }}>{column.logical_type}</small>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, rowIndex) => (
                    <tr key={rowIndex} style={{ borderBottom: '1px solid var(--color-border-light)' }}>
                      {(Array.isArray(row) ? row : []).map((cell, cellIndex) => (
                        <td key={cellIndex} title={cell?.kind ?? undefined} style={{ padding: '7px 10px', fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)', whiteSpace: 'pre-wrap' }}>{renderedTypedValue(cell)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, padding: '8px 11px', borderTop: '1px solid var(--color-border-light)' }}>
            <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{result.returned_row_count ?? rows.length} rows{partial ? ' · partial' : ' · complete'}</span>
            {result.duration_ms !== undefined && <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{result.duration_ms}ms</span>}
            {result.session_time_zone && <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>Time zone: {result.session_time_zone}</span>}
            {ordering.length > 0 && <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>Order: {ordering.map(item => `${item.alias} ${item.direction}`).join(', ')}</span>}
          </div>
        </>
      )}
    </section>
  )
}

const card = { padding: '10px 12px', borderRadius: 8, fontFamily: 'var(--font-body)', fontSize: 12 }

function prefersReducedMotion() {
  try {
    return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true
  } catch {
    return false
  }
}

function AiSettingsBlock({ settings }) {
  if (!settings) return null
  const onOff = value => (value ? 'On' : 'Off')
  return (
    <div style={{ marginBottom: 20 }}>
      <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>AI settings</p>
      <div style={{ background: 'white', border: '1px solid var(--color-border)', borderRadius: 8, padding: '10px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 1.55 }}>
        <div>Result rows sent to the AI model: <strong>{onOff(settings.result_rows_to_model)}</strong></div>
        <div>Example values in questions: <strong>{onOff(settings.sample_values_enabled)}</strong></div>
        <Link to="/upload" style={{ display: 'inline-block', marginTop: 6, fontSize: 12 }}>Change</Link>
      </div>
    </div>
  )
}

function DatabaseChatSession({ sourceId, initialPrefill }) {
  const [sessionId, setSessionId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState(initialPrefill)
  const [streaming, setStreaming] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [resultReceived, setResultReceived] = useState(false)
  const [sessionError, setSessionError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [newConversationError, setNewConversationError] = useState(null)
  const [aiSettings, setAiSettings] = useState(null)
  const [unpinned, setUnpinned] = useState(false)
  const [sourceContext, setSourceContext] = useState({ displayName: null, snapshotId: null })
  const listRef = useRef(null)
  const composerRef = useRef(null)
  const pinnedRef = useRef(true)
  const streamAbortRef = useRef(null)
  const turnIdRef = useRef(null)
  const resultReceivedRef = useRef(false)
  const focusAfterTurnRef = useRef(false)
  const sessionIdRef = useRef(null)

  const scrollToBottom = useCallback(() => {
    const element = listRef.current
    if (!element) return
    if (prefersReducedMotion() || typeof element.scrollTo !== 'function') element.scrollTop = element.scrollHeight
    else element.scrollTo({ top: element.scrollHeight, behavior: 'smooth' })
  }, [])

  // SPEC-10 W6.3: follow the conversation only while the reader is at the bottom.
  useEffect(() => {
    if (pinnedRef.current) scrollToBottom()
  }, [messages, scrollToBottom])

  const handleScroll = () => {
    const element = listRef.current
    if (!element) return
    const pinned = element.scrollHeight - element.scrollTop - element.clientHeight < 120
    pinnedRef.current = pinned
    setUnpinned(!pinned)
  }

  const jumpToLatest = () => {
    pinnedRef.current = true
    setUnpinned(false)
    scrollToBottom()
  }

  useEffect(() => { sessionIdRef.current = sessionId }, [sessionId])

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false

    streamAbortRef.current?.abort()
    turnIdRef.current = null
    resultReceivedRef.current = false
    const resolveSourceContext = async () => {
      const [source, catalog] = await Promise.all([
        sourceApi.getSource(sourceId, { signal: controller.signal }),
        sourceApi.getCatalog(sourceId, { signal: controller.signal }),
      ])
      const snapshotId = catalog?.current_snapshot_id
      if (!snapshotId) {
        throw Object.assign(new Error('catalog_not_found'), { code: 'catalog_not_found' })
      }
      return {
        // These are safe source/catalog metadata responses. The client still
        // never uses either value as authority for session creation or query
        // execution; the source-bound endpoints decide that server-side.
        displayName: source?.display_name ?? 'Selected PostgreSQL source',
        snapshotId,
      }
    }

    const applySession = (session, transcript, presentation) => {
      if (disposed || controller.signal.aborted || !isSourceSession(session, sourceId)) return false
      const persistedMessages = transcript?.messages ?? []
      saveSessionId(sourceId, session.session_id)
      setSessionId(session.session_id)
      setMessages(persistedMessages.map(toDisplayMessage))
      setSourceContext(presentation)
      return true
    }

    const restoreSession = async (session, presentation) => {
      const transcript = await sourceApi.getChatMessages(session.session_id, { limit: 500 }, { signal: controller.signal })
      return applySession(session, transcript, presentation)
    }

    const createSession = async presentation => {
      const created = await sourceApi.createChatSession(sourceId, { mode: 'AUTO' }, { signal: controller.signal })
      const session = created?.session
      if (!isSourceSession(session, sourceId)) throw new Error('source_session_mismatch')
      applySession(session, { messages: [] }, presentation)
    }

    const restoreOrCreateSession = async () => {
      // This presentation fetch is not authorization. It prevents a direct
      // browser URL from creating a chat shell when no current published
      // snapshot exists; the server rechecks access and freshness separately.
      const presentation = await resolveSourceContext()
      const savedSessionId = readSavedSessionId(sourceId)
      if (savedSessionId) {
        try {
          const response = await sourceApi.getChatSession(savedSessionId, { signal: controller.signal })
          const savedSession = response?.session
          if (isSourceSession(savedSession, sourceId)) {
            await restoreSession(savedSession, presentation)
            return
          }
          clearSavedSessionId(sourceId)
        } catch (error) {
          if (controller.signal.aborted) return
          clearSavedSessionId(sourceId)
          // A 404 here can only refer to a stale browser-saved session: the
          // current source and catalog were resolved above. Continue through
          // the source-bound list endpoint, which re-authorizes the source
          // before deciding whether restoring or creating is permitted.
          const code = sourceErrorCode(error) || error?.code
          const staleSavedSession = error?.status === 404
          if (!staleSavedSession && ([401, 403].includes(error?.status) || SAFE_SOURCE_ERRORS[code])) throw error
        }
      }

      const response = await sourceApi.listChatSessions(sourceId, { limit: 50 }, { signal: controller.signal })
      const sessions = (response?.sessions ?? []).filter(session => isSourceSession(session, sourceId))
      const previous = sessions.find(session => session.message_count > 0) ?? sessions[0]
      if (previous) await restoreSession(previous, presentation)
      else await createSession(presentation)
    }

    restoreOrCreateSession().catch(error => {
      if (!disposed && !controller.signal.aborted) {
        setSessionError(safeErrorMessage(error))
      }
    })

    return () => {
      disposed = true
      controller.abort()
      streamAbortRef.current?.abort()
    }
  }, [sourceId])

  // SPEC-10 W6.6: the owner's AI settings, read-only. Hidden when they cannot be read; refreshed when the window
  // regains focus because they may have been changed in another tab.
  useEffect(() => {
    let disposed = false
    const load = () => {
      Promise.resolve()
        .then(() => sourceApi.getAiSettings(sourceId))
        .then(settings => { if (!disposed) setAiSettings(settings && typeof settings === 'object' ? settings : null) })
        .catch(() => { if (!disposed) setAiSettings(null) })
    }
    load()
    window.addEventListener('focus', load)
    return () => {
      disposed = true
      window.removeEventListener('focus', load)
    }
  }, [sourceId])

  const patchMessage = useCallback((assistantId, patch) => {
    setMessages(current => current.map(message => message.id === assistantId
      ? { ...message, ...(typeof patch === 'function' ? patch(message) : patch) }
      : message))
  }, [])

  // SPEC-10 W1.7. The server transcript is the source of truth after any turn that ended without `done`. Read only:
  // nothing is ever re-sent. Returns the transcript's messages, or null when it could not be read.
  const reconcile = useCallback(async () => {
    const id = sessionIdRef.current
    if (!id) return null
    try {
      const transcript = await sourceApi.getChatMessages(id, { limit: 500 })
      const stored = transcript?.messages ?? []
      if (sessionIdRef.current !== id) return null
      setMessages(stored.map(toDisplayMessage))
      return stored
    } catch {
      return null // keep the local view
    }
  }, [])

  const handleSend = useCallback(async (override) => {
    const question = (typeof override === 'string' ? override : input).trim()
    if (!question || streaming) return
    if (question.length > MAX_QUESTION_CHARS) return
    if (!sessionId || !sourceId) {
      setSessionError('This database conversation is not ready yet.')
      return
    }

    const stamp = Date.now()
    const userId = `user-${stamp}`
    const assistantId = `assistant-${stamp}`
    const storedUserCount = messages.filter(message => message.role === 'user' && !String(message.id).startsWith('user-')).length
    const streamController = new AbortController()
    streamAbortRef.current?.abort()
    streamAbortRef.current = streamController
    turnIdRef.current = null
    resultReceivedRef.current = false
    let accepted = false
    let sawDone = false
    let sawError = false
    setResultReceived(false)
    setNotice(null)
    pinnedRef.current = true
    setUnpinned(false)
    setMessages(current => [
      ...current,
      { id: userId, role: 'user', text: question, streaming: false },
      { id: assistantId, role: 'assistant', text: '', postgresResult: null, answer: null, terminalText: null, outcome: 'pending', stage: 'sending', reasonCode: null, errorCode: null, startedAt: stamp, streaming: true },
    ])
    setInput('')
    setStreaming(true)
    setCancelling(false)
    setSessionError(null)

    try {
      const stream = sourceApi.streamChatMessage(sessionId, { message: question }, { signal: streamController.signal })
      for await (const { event, data } of stream) {
        if (streamController.signal.aborted) break
        accepted = true                       // any frame proves the server took the turn
        if (event === 'start') {
          turnIdRef.current = typeof data?.turn_id === 'string' ? data.turn_id : null
          patchMessage(assistantId, { stage: 'understanding' })
        } else if (event === 'planning') {
          patchMessage(assistantId, { stage: 'understanding' })
        } else if (event === 'catalog') {
          setSourceContext(current => ({ ...current, snapshotId: data?.snapshot_id ?? current.snapshotId }))
        } else if (event === 'sql_generated') {
          // The owner sees the query (decision 6). A second one means the first was corrected once.
          patchMessage(assistantId, message => {
            const previous = message.answer?.sql ?? null
            const corrected = Boolean(message.answer?.repaired) || data?.attempt >= 2
            return {
              stage: corrected ? 'corrected' : 'written',
              answer: {
                ...(message.answer ?? {}),
                sql: typeof data?.sql === 'string' ? data.sql : null,
                firstSql: corrected && previous ? previous : message.answer?.firstSql ?? null,
                assumptions: Array.isArray(data?.assumptions) ? data.assumptions : [],
                route: 'sql',
              },
            }
          })
        } else if (event === 'repairing') {
          patchMessage(assistantId, message => ({
            stage: 'repairing',
            reasonCode: data?.reason_code ?? null,
            answer: { ...(message.answer ?? {}), repaired: true, repair: { reasonCode: data?.reason_code ?? null } },
          }))
        } else if (event === 'executing') {
          patchMessage(assistantId, { stage: 'running' })
        } else if (event === 'presenting') {
          patchMessage(assistantId, { stage: 'presenting' })
        } else if (event === 'presentation') {
          patchMessage(assistantId, message => ({ answer: { ...(message.answer ?? {}), presentation: data && typeof data === 'object' ? data : null } }))
        } else if (event === 'clarification' || event === 'clarification_required') {
          const terminalText = data?.message ?? data?.question ?? 'Please clarify which table or calculation you mean.'
          focusAfterTurnRef.current = true
          patchMessage(assistantId, { text: terminalText, terminalText, stage: null, outcome: 'clarification' })
        } else if (event === 'unsupported') {
          const terminalText = data?.message ?? 'That request is not supported for this database conversation.'
          patchMessage(assistantId, { text: terminalText, terminalText, stage: null, outcome: 'unsupported' })
        } else if (event === 'postgres_result') {
          const postgresResult = data?.postgres_result ?? data?.result ?? data
          if (postgresResult) {
            resultReceivedRef.current = true
            setResultReceived(true)
          }
          patchMessage(assistantId, { postgresResult, stage: null })
        } else if (event === 'token') {
          const tokenText = data?.text ?? ''
          patchMessage(assistantId, message => {
            const duplicateTerminalToken = Boolean(message.terminalText) && message.terminalText === tokenText
            return { text: duplicateTerminalToken ? message.text : message.text + tokenText, terminalText: null, stage: null }
          })
        } else if (event === 'error') {
          sawError = true
          if (resultReceivedRef.current) {
            patchMessage(assistantId, { text: 'The database result above was received, but the response ended before its final summary.', stage: null, outcome: 'interrupted' })
          } else {
            const text = safeEventMessage(data, GENERIC_FAILURE)
            patchMessage(assistantId, { text, terminalText: text, errorCode: data?.code ?? null, postgresResult: null, stage: null, outcome: 'failed' })
          }
        } else if (event === 'done') {
          sawDone = true
          const result = data?.postgres_result ?? data?.result
          if (result) {
            resultReceivedRef.current = true
            setResultReceived(true)
          }
          patchMessage(assistantId, message => ({
            postgresResult: result ?? message.postgresResult,
            stage: null,
            streaming: false,
            outcome: message.outcome === 'pending' ? 'answer' : message.outcome,
          }))
          break // nothing after `done` changes the message
        }
        // Unknown events are ignored.
      }
      if (!sawDone && !streamController.signal.aborted && accepted) {
        patchMessage(assistantId, message => ({
          streaming: false, stage: null,
          outcome: message.outcome === 'pending' ? (message.postgresResult ? 'interrupted' : 'interrupted') : message.outcome,
          text: message.outcome === 'pending' && !message.text ? 'The connection was lost while answering.' : message.text,
        }))
        if (!sawError) void reconcile()
      }
    } catch (error) {
      const cancelledByUser = streamController.signal.aborted
      if (!accepted && !cancelledByUser) {
        // Pre-acceptance: the server never took the question. Remove the bubbles, give the question back, say why.
        const failure = noticeFor(error)
        setMessages(current => current.filter(message => message.id !== userId && message.id !== assistantId))
        setInput(current => (current.trim() ? current : question))
        setNotice(failure)
        if (failure.reconcile) {
          const stored = await reconcile()
          // The server may have accepted the question after all: then it is in the transcript, not in the box.
          const storedCount = stored ? stored.filter(message => String(message.role).toLowerCase() === 'user').length : 0
          if (stored && storedCount > storedUserCount) setInput(current => (current === question ? '' : current))
        }
      } else if (cancelledByUser) {
        if (resultReceivedRef.current) {
          patchMessage(assistantId, { text: 'Cancelled.', stage: null, outcome: 'interrupted', streaming: false })
        } else {
          patchMessage(assistantId, { text: 'Cancelled.', postgresResult: null, stage: null, outcome: 'cancelled', streaming: false })
        }
        void reconcile()
      } else if (resultReceivedRef.current) {
        patchMessage(assistantId, { text: 'The database result above was received before the response ended.', stage: null, outcome: 'interrupted', streaming: false })
        void reconcile()
      } else {
        patchMessage(assistantId, { text: 'The connection was lost while answering.', stage: null, outcome: 'interrupted', streaming: false })
        void reconcile()
      }
    } finally {
      if (streamAbortRef.current === streamController) {
        streamAbortRef.current = null
        turnIdRef.current = null
      }
      setStreaming(false)
      setCancelling(false)
    }
  }, [input, messages, patchMessage, reconcile, sessionId, sourceId, streaming])

  useEffect(() => {
    if (!streaming && focusAfterTurnRef.current) {
      focusAfterTurnRef.current = false
      const active = document.activeElement
      const typing = active && active !== document.body && active !== composerRef.current && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)
      if (!typing) composerRef.current?.focus()
    }
  }, [streaming])

  const cancelExecution = useCallback(() => {
    const streamController = streamAbortRef.current
    if (!streamController || cancelling || resultReceivedRef.current) return
    setCancelling(true)
    const turnId = turnIdRef.current
    if (turnId && sessionId) {
      // This is a one-shot acknowledgement, never a retry mechanism. Invoke
      // it before closing the SSE stream so a live server-side turn can ask
      // Stage 4B to terminate its dedicated database connection.
      void sourceApi.cancelChatTurn(sessionId, turnId).catch(() => {
        // A disconnect remains the bounded fallback when the acknowledgement
        // cannot be confirmed; do not expose transport details or retry.
      })
    }
    streamController.abort()
  }, [cancelling, sessionId])

  const startNewConversation = async () => {
    if (streaming) return
    setNewConversationError(null)
    try {
      const created = await sourceApi.createChatSession(sourceId, { mode: 'AUTO' })
      const session = created?.session
      if (!isSourceSession(session, sourceId)) throw new Error('source_session_mismatch')
      saveSessionId(sourceId, session.session_id)
      setSessionId(session.session_id)
      setMessages([])
      setNotice(null)
      pinnedRef.current = true
      composerRef.current?.focus()
    } catch (error) {
      setNewConversationError(safeErrorMessage(error, 'We could not start a new conversation. Your current conversation is unchanged.'))
    }
  }

  const editQuestion = messageId => {
    const index = messages.findIndex(message => message.id === messageId)
    const previousUser = messages.slice(0, index).reverse().find(message => message.role === 'user')
    if (!previousUser) return
    setInput(previousUser.text)
    composerRef.current?.focus()
  }

  const tryAgain = messageId => {
    const index = messages.findIndex(message => message.id === messageId)
    const previousUser = messages.slice(0, index).reverse().find(message => message.role === 'user')
    if (previousUser) void handleSend(previousUser.text)       // a new turn, started by the user's click
  }

  const handleKeyDown = event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void handleSend()
    }
  }

  const displayName = sourceContext.displayName || 'Selected PostgreSQL source'
  const sessionReady = Boolean(sessionId && !sessionError)
  const tooLong = input.length > MAX_QUESTION_CHARS
  const canSend = Boolean(input.trim()) && sessionReady && !streaming && !tooLong

  const renderAssistant = (message) => {
    const sqlAnswer = Boolean(message.postgresResult) && message.answer?.route === 'sql'
    const sqlOutcome = message.outcome
    const proseHidden = sqlAnswer || ['clarification', 'unsupported', 'failed', 'cancelled', 'interrupted'].includes(sqlOutcome)
    return (
      <div className="chat-assistant">
        {message.outcome === 'pending' && (
          <TurnProgress stage={message.stage} startedAt={message.startedAt} sourceName={displayName} reasonCode={message.reasonCode} typed={!message.answer} />
        )}
        {message.outcome === 'clarification' && <ClarificationCard text={message.text} />}
        {message.outcome === 'unsupported' && <UnsupportedCard text={message.text} writeRefusal={message.text === WRITE_REFUSAL} />}
        {message.outcome === 'failed' && (
          <FailedCard text={message.text} answer={message.answer} failureCode={message.errorCode} busy={streaming}
            onEdit={() => editQuestion(message.id)} onTryAgain={() => tryAgain(message.id)} />
        )}
        {message.outcome === 'cancelled' && <CancelledNote text={message.text || 'Cancelled.'} />}
        {message.outcome === 'interrupted' && <InterruptedNote text={message.text} />}
        {message.text && !proseHidden && (
          <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)', lineHeight: 1.6, marginBottom: message.postgresResult ? 10 : 0, whiteSpace: 'pre-wrap' }}>
            {message.text}{message.streaming && <span style={{ display: 'inline-block', width: 8, height: 14, background: 'var(--color-primary-indigo)', marginLeft: 2, borderRadius: 1, verticalAlign: 'text-bottom', animation: 'pulse-dot 0.8s ease-in-out infinite' }} />}
          </p>
        )}
        {sqlAnswer
          ? <AnswerView result={message.postgresResult} answer={message.answer} text={message.text} />
          : <PostgresResultTable result={message.postgresResult} />}
      </div>
    )
  }

  return (
    <Layout className="chat-page" title="Database Conversation" subtitle="Authorized catalog context and controlled PostgreSQL reads">
      <div className="chat-workspace" style={{ display: 'grid', gridTemplateColumns: '1fr 260px', gap: 0, height: 'calc(100vh - var(--topbar-height) - 48px)', position: 'relative' }}>
        <div className="chat-conversation" style={{ display: 'flex', flexDirection: 'column', background: 'transparent', minHeight: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8, marginBottom: 12, paddingRight: 12 }}>
            <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={() => { void startNewConversation() }} disabled={streaming || !sessionReady}>New conversation</button>
            <span style={{ background: '#EFF6FF', color: '#1D4ED8', border: '1px solid #BFDBFE', borderRadius: 'var(--radius-full)', padding: '5px 12px', fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Database mode</span>
          </div>
          {newConversationError && <div role="alert" style={{ ...card, marginBottom: 8, marginRight: 12, background: '#FFF1F2', border: '1px solid #FECDD3', color: '#9F1239' }}>{newConversationError}</div>}

          <div style={{ position: 'relative', flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <div ref={listRef} onScroll={handleScroll} className="chat-messages" style={{ flex: 1, overflowY: 'auto', paddingRight: 12, display: 'flex', flexDirection: 'column', gap: 16 }}>
              {sessionError && <div role="alert" style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 8, padding: '12px 16px', fontFamily: 'var(--font-body)', fontSize: 13, color: '#9F1239' }}>{sessionError}</div>}
              {messages.length === 0 ? (
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: '40px 24px', gap: 12 }}>
                  <div style={{ width: 52, height: 52, borderRadius: '50%', background: '#EFF6FF', border: '1px solid #BFDBFE', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--color-primary-indigo)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
                  </div>
                  {!sessionReady && !sessionError ? (
                    <><h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)' }}>Creating database session…</h3><p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', maxWidth: 360, lineHeight: 1.6 }}>Authorizing the selected source and its published metadata.</p></>
                  ) : sessionError ? (
                    <><h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)' }}>Database chat is unavailable</h3><Link to="/upload" className="btn btn-primary btn-sm" style={{ marginTop: 8 }}>Go to sources</Link></>
                  ) : (
                    <><h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)' }}>Ask about {displayName}</h3><p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', maxWidth: 410, lineHeight: 1.6 }}>Ask a question in plain language. I write a read-only query over the tables this source shares, run it, and show the answer with the exact result table and the SQL I used. Confirmed metrics are calculated with their approved definitions.</p></>
                  )}
                </div>
              ) : messages.map(message => (
                <div key={message.id} className="anim-fade-up">
                  {message.role === 'user' ? (
                    <div style={{ display: 'flex', justifyContent: 'flex-end' }}><div className="chat-user">{message.text}</div></div>
                  ) : (
                    <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                      <div style={{ width: 28, height: 28, borderRadius: 8, background: 'var(--color-primary)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: 4 }}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3" /><path d="M3 12h3M18 12h3M12 3v3M12 18v3" /></svg></div>
                      <div style={{ flex: 1, maxWidth: '85%', minWidth: 0 }}>{renderAssistant(message)}</div>
                    </div>
                  )}
                </div>
              ))}
            </div>
            {unpinned && streaming && (
              <button type="button" className="btn btn-primary btn-sm" onClick={jumpToLatest} style={{ position: 'absolute', bottom: 8, left: '50%', transform: 'translateX(-50%)', borderRadius: 999 }}>Jump to latest</button>
            )}
          </div>

          {notice && (
            <div role="alert" style={{ ...card, marginTop: 12, background: '#FFFBEB', border: '1px solid #FDE68A', color: '#92400E', display: 'flex', gap: 10, alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap' }}>
              <span>{notice.text}</span>
              <span style={{ display: 'flex', gap: 8 }}>
                {notice.refresh && <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={() => { void reconcile() }}>Refresh conversation</button>}
                <button type="button" className="btn btn-ghost-neutral btn-sm" aria-label="Dismiss notice" onClick={() => setNotice(null)}>Dismiss</button>
              </span>
            </div>
          )}

          <div className="chat-composer" style={{ marginTop: 12, background: 'white', border: '1px solid var(--color-border)', borderRadius: 12, padding: '12px 14px', boxShadow: 'var(--shadow-card)' }}>
            <textarea ref={composerRef} aria-label="Database question" value={input} onChange={event => { setInput(event.target.value); if (notice) setNotice(null) }} onKeyDown={handleKeyDown} placeholder={sessionReady ? 'Ask a question about this database source…' : 'Preparing database conversation…'} rows={2} disabled={!sessionReady || streaming} style={{ width: '100%', border: 'none', outline: 'none', resize: 'none', fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)', lineHeight: 1.5, background: 'transparent', opacity: sessionReady ? 1 : 0.5 }} />
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--color-border-light)' }}>
              <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: tooLong ? '#9F1239' : 'var(--color-text-muted)' }} role={tooLong ? 'alert' : undefined}>
                {tooLong ? QUESTION_TOO_LONG : streaming ? (cancelling ? 'Cancelling controlled query…' : 'Working on it…') : 'Read-only · answers show their SQL'}
                {input.length > 3500 && !tooLong && <span> · {input.length}/{MAX_QUESTION_CHARS}</span>}
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                {streaming && !resultReceived && <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={cancelExecution} disabled={cancelling}>{cancelling ? 'Cancelling…' : 'Cancel'}</button>}
                <button type="button" aria-label="Send database question" onClick={() => { void handleSend() }} disabled={!canSend} style={{ width: 32, height: 32, borderRadius: 8, background: canSend ? 'var(--color-primary-indigo)' : 'var(--color-border)', border: 'none', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: canSend ? 'pointer' : 'not-allowed' }}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" /></svg></button>
              </div>
            </div>
          </div>
        </div>

        <aside className="chat-context" style={{ paddingLeft: 16, borderLeft: '1px solid var(--color-border)', overflowY: 'auto' }} aria-label="Database session context">
          <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 14, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 14 }}>Session Context</h3>
          <div style={{ marginBottom: 20 }}>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Database source</p>
            <div style={{ background: 'white', border: '1px solid var(--color-border)', borderRadius: 8, padding: '10px 12px' }}>
              <strong style={{ display: 'block', fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)', overflowWrap: 'anywhere' }}>{displayName}</strong>
              <span style={{ display: 'block', marginTop: 4, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>PostgreSQL · authorized server-side</span>
              {sourceContext.snapshotId && <code style={{ display: 'block', marginTop: 7, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-secondary)', overflowWrap: 'anywhere' }}>Snapshot: {sourceContext.snapshotId}</code>}
            </div>
          </div>
          <div style={{ marginBottom: 20 }}>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Capability</p>
            <div style={{ background: 'white', border: '1px solid var(--color-border)', borderRadius: 8, padding: '10px 12px', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 1.55 }}>Read-only questions across the tables this source shares. Confirmed metrics use their approved definitions. Every answer shows its SQL and the exact rows.</div>
          </div>
          <AiSettingsBlock settings={aiSettings} />
          <div style={{ marginTop: 20, padding: '12px', background: 'var(--color-base)', borderRadius: 8 }}>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Session stats</p>
            {[['Messages', String(messages.length)], ['Session ID', sessionId ? `${sessionId.slice(0, 8)}…` : '—'], ['Binding', 'Database source']].map(([label, value]) => <div key={label} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 4 }}><span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{label}</span><span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)', overflowWrap: 'anywhere', textAlign: 'right' }}>{value}</span></div>)}
          </div>
        </aside>
      </div>
    </Layout>
  )
}

export default function DatabaseChat() {
  const { sourceId } = useParams()
  const location = useLocation()
  if (!sourceId) {
    return (
      <Layout className="chat-page" title="Database Conversation" subtitle="Authorized catalog context and controlled PostgreSQL reads">
        <div className="card card-pad" role="alert">No database source was selected.</div>
      </Layout>
    )
  }
  // A source change remounts the session workspace rather than carrying any
  // old source state or active stream across an authorization boundary.
  return <DatabaseChatSession key={sourceId} sourceId={sourceId} initialPrefill={location.state?.prefill || ''} />
}
