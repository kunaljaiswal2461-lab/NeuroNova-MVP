import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { sourceApi, sourceErrorCode } from '../../api/sources'

const EMPTY_FORM = {
  name: '',
  description: '',
  operation: 'count',
  aggregateColumnId: '',
  unitLabel: '',
  defaultDateColumnId: '',
  defaultTimeZone: '',
}

const AGGREGATES = [
  { value: 'count', label: 'Count rows', needsColumn: false },
  { value: 'sum', label: 'Sum', needsColumn: true },
  { value: 'average', label: 'Average', needsColumn: true },
  { value: 'minimum', label: 'Minimum', needsColumn: true },
  { value: 'maximum', label: 'Maximum', needsColumn: true },
]

const NUMERIC_TYPES = new Set(['int2', 'int4', 'int8', 'numeric', 'float4', 'float8'])
const TEMPORAL_TYPES = new Set(['date', 'timestamp', 'timestamptz'])

const SAFE_ERROR_MESSAGES = {
  authentication_required: 'Your sign-in session is unavailable. Please sign in again to manage metric definitions.',
  source_not_found: 'This saved connection is no longer available.',
  catalog_not_found: 'Publish metadata before creating or revising a metric definition.',
  catalog_snapshot_not_found: 'The displayed metadata snapshot is no longer available. Refresh metadata and try again.',
  catalog_scope_changed: 'The selected table scope changed. Refresh metadata before saving a metric definition.',
  catalog_asset_not_found: 'The selected table is not available in the current authorized metadata.',
  metric_definition_not_found: 'That metric definition is no longer available.',
  metric_definition_conflict: 'A metric definition with that name already exists for this source.',
  metric_definition_needs_review: 'This metric needs review against the current metadata before it can be changed or used.',
  metric_definition_retired: 'This metric definition has been retired.',
  metric_catalog_unavailable: 'Current authorized metadata is unavailable for this metric. Refresh metadata and try again.',
  metric_definition_persistence_failed: 'The metric definition could not be saved. Your draft is still available to review.',
  metric_snapshot_changed: 'Metadata changed while this definition was being prepared. Review the current table and try again.',
  invalid_metric_definition: 'The metric definition is not compatible with the current authorized metadata.',
}

function safeErrorMessage(error, fallback = 'We could not complete that metric definition request. Please try again.') {
  return SAFE_ERROR_MESSAGES[sourceErrorCode(error)] || fallback
}

function quoteIdentifier(identifier) {
  return `"${String(identifier || '').replace(/"/g, '""')}"`
}

function assetLabel(asset) {
  if (!asset) return 'Selected table'
  return `${quoteIdentifier(asset.schema_name)}.${quoteIdentifier(asset.table_name)}`
}

function assetId(asset) {
  return asset?.asset_id ?? asset?.id ?? null
}

function columnId(column) {
  // Catalog IDs are opaque snapshot-bound references. Do not fall back to a
  // column name: names are display text and must never become a semantic
  // identity at this boundary.
  return column?.column_id ?? column?.id ?? null
}

function columnName(column) {
  return column?.column_name ?? column?.name ?? 'Unnamed column'
}

function nativeType(column) {
  return String(column?.native_type_name ?? column?.native_type ?? '').toLowerCase()
}

function valueKind(column) {
  const type = nativeType(column)
  if (type === 'int2' || type === 'int4' || type === 'int8') return 'integer'
  if (type === 'numeric') return 'decimal'
  if (type === 'float4' || type === 'float8') return 'number'
  if (type === 'bool' || type === 'boolean') return 'boolean'
  if (type === 'uuid') return 'uuid'
  if (type === 'date') return 'date'
  if (type === 'timestamp') return 'timestamp'
  if (type === 'timestamptz') return 'timestamptz'
  return 'text'
}

function isNumeric(column) {
  return NUMERIC_TYPES.has(nativeType(column))
}

function isTemporal(column) {
  return TEMPORAL_TYPES.has(nativeType(column))
}

function aggregateNeedsColumn(operation) {
  return operation !== 'count'
}

function eligibleAggregateColumns(columns, operation) {
  if (operation === 'sum' || operation === 'average') return columns.filter(isNumeric)
  if (operation === 'minimum' || operation === 'maximum') {
    return columns.filter(column => isNumeric(column) || isTemporal(column))
  }
  return []
}

function filterOperators(column) {
  const kind = valueKind(column)
  const common = [
    { value: 'eq', label: 'equals' },
    { value: 'neq', label: 'does not equal' },
  ]
  const nulls = [
    { value: 'is_null', label: 'is null' },
    { value: 'is_not_null', label: 'is not null' },
  ]
  if (kind === 'integer' || kind === 'decimal' || kind === 'number' || isTemporal(column)) {
    return [...common,
      { value: 'lt', label: 'is before / less than' },
      { value: 'lte', label: 'is at most' },
      { value: 'gt', label: 'is after / greater than' },
      { value: 'gte', label: 'is at least' },
      ...nulls,
    ]
  }
  if (kind === 'text') return [...common, { value: 'contains', label: 'contains' }, { value: 'not_contains', label: 'does not contain' }, ...nulls]
  return [...common, ...nulls]
}

function isNullOperator(operator) {
  return operator === 'is_null' || operator === 'is_not_null'
}

function displayOperation(operation) {
  return AGGREGATES.find(item => item.value === operation)?.label || operation || 'Unknown aggregate'
}

function displayState(state) {
  const normalized = String(state || '').toLowerCase()
  if (normalized === 'valid') return 'Active'
  if (normalized === 'needs_review') return 'Needs review'
  if (normalized === 'retired') return 'Retired'
  return 'Status unavailable'
}

function stateClass(state) {
  const normalized = String(state || '').toLowerCase()
  if (normalized === 'valid') return 'metric-state-valid'
  if (normalized === 'needs_review') return 'metric-state-review'
  if (normalized === 'retired') return 'metric-state-retired'
  return 'metric-state-unknown'
}

function dateText(value) {
  if (!value) return 'Confirmation time unavailable'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? 'Confirmation time unavailable' : parsed.toLocaleString()
}

function metricFromResponse(response) {
  return response?.metric ?? response?.item ?? response
}

function metricItems(response) {
  const items = response?.items ?? response?.metrics ?? response
  return Array.isArray(items) ? items : []
}

function metricVersions(response) {
  const versions = response?.versions ?? response?.items ?? response
  return Array.isArray(versions) ? versions : []
}

function currentVersion(metric, versions = []) {
  if (metric?.current_version) return metric.current_version
  if (metric?.version_number) return metric
  return [...versions].sort((left, right) => (right.version_number ?? 0) - (left.version_number ?? 0))[0] ?? null
}

function typedValue(column, rawValue) {
  const kind = valueKind(column)
  const value = String(rawValue ?? '')
  if (kind === 'boolean') {
    if (value !== 'true' && value !== 'false') return { error: 'Choose true or false for this filter.' }
    return { value: { kind, value: value === 'true' } }
  }
  if (kind === 'integer') {
    if (!/^-?\d+$/.test(value)) return { error: 'Enter a whole-number value for this filter.' }
    const parsed = Number(value)
    // JSON cannot carry BigInt and sending a rounded integer would silently
    // redefine a confirmed filter. Make the browser boundary fail closed.
    if (!Number.isSafeInteger(parsed)) return { error: 'This integer is outside the browser-safe range. Use a supported exact value before confirming.' }
    return { value: { kind, value: parsed } }
  }
  if (kind === 'decimal') {
    if (!/^-?(?:\d+|\d*\.\d+)$/.test(value)) return { error: 'Enter a finite decimal value for this filter.' }
    return { value: { kind, value } }
  }
  if (kind === 'number') {
    const parsed = Number(value)
    if (!Number.isFinite(parsed)) return { error: 'Enter a finite number for this filter.' }
    return { value: { kind, value: parsed } }
  }
  if (!value && kind !== 'text') return { error: 'Enter a value for this filter.' }
  return { value: { kind, value } }
}

function filterValueForForm(filter) {
  if (!filter || isNullOperator(filter.operator)) return ''
  const value = filter.value
  if (!value || typeof value !== 'object') return ''
  return value.value == null ? '' : String(value.value)
}

function filterSummary(filter, columns) {
  const column = columns.find(item => String(columnId(item)) === String(filter.column_id))
  const operator = filterOperators(column).find(item => item.value === filter.operator)?.label || filter.operator
  if (isNullOperator(filter.operator)) return `${quoteIdentifier(columnName(column))} ${operator}`
  const typed = typedValue(column, filter.value)
  if (typed.error) return `${quoteIdentifier(columnName(column))} ${operator} a value`
  const value = typed.value.value
  const formatted = typeof value === 'string' ? `“${value}”` : String(value)
  return `${quoteIdentifier(columnName(column))} ${operator} ${formatted}`
}

function definitionSummary({ asset, operation, aggregateColumnId, filters, columns, unitLabel, defaultDateColumnId, defaultTimeZone }) {
  const aggregateColumn = columns.find(column => String(columnId(column)) === String(aggregateColumnId))
  const aggregate = operation === 'count'
    ? 'Count rows'
    : `${displayOperation(operation)} of ${quoteIdentifier(columnName(aggregateColumn))}`
  const parts = [`${aggregate} in ${assetLabel(asset)}`]
  if (filters.length) parts.push(`where ${filters.map(filter => filterSummary(filter, columns)).join(' and ')}`)
  if (unitLabel.trim()) parts.push(`unit: ${unitLabel.trim()}`)
  const defaultDateColumn = columns.find(column => String(columnId(column)) === String(defaultDateColumnId))
  if (defaultDateColumn) {
    parts.push(`default time basis: ${quoteIdentifier(columnName(defaultDateColumn))}${defaultTimeZone.trim() ? ` (${defaultTimeZone.trim()})` : ''}`)
  }
  return `${parts.join(' · ')}.`
}

function MetricCard({ metric, onInspect, loading }) {
  const version = currentVersion(metric)
  const state = metric?.state
  return (
    <article className="metric-definition-card">
      <div className="metric-definition-card-heading">
        <div>
          <h5>{metric?.name || metric?.display_name || 'Unnamed metric'}</h5>
          <p>{version?.version_number ? `Version ${version.version_number}` : 'Version unavailable'}</p>
        </div>
        <span className={`metric-state-chip ${stateClass(state)}`}>{displayState(state)}</span>
      </div>
      {metric?.description && <p className="metric-definition-description">{metric.description}</p>}
      <dl className="metric-definition-summary">
        <div><dt>Aggregate</dt><dd>{displayOperation(version?.aggregation?.operation)}</dd></div>
        <div><dt>Unit</dt><dd>{version?.unit_label || '—'}</dd></div>
        <div><dt>Confirmed</dt><dd>{dateText(version?.confirmed_at ?? metric?.confirmed_at)}</dd></div>
      </dl>
      <div className="source-actions">
        <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => onInspect(metric.metric_id)} disabled={loading}>
          {loading ? 'Loading…' : 'Inspect definition'}
        </button>
      </div>
    </article>
  )
}

export default function MetricDefinitionsPanel({ sourceId, snapshotId, selectedAssetDetail }) {
  const [open, setOpen] = useState(false)
  const [metrics, setMetrics] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [selectedMetric, setSelectedMetric] = useState(null)
  const [versions, setVersions] = useState([])
  const [inspecting, setInspecting] = useState(false)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editingMetric, setEditingMetric] = useState(null)
  const [draftContext, setDraftContext] = useState(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [filters, setFilters] = useState([])
  const [confirmed, setConfirmed] = useState(false)
  const [saving, setSaving] = useState(false)
  const [retireTarget, setRetireTarget] = useState(null)
  const [retiring, setRetiring] = useState(false)

  const mountedRef = useRef(false)
  const listControllerRef = useRef(null)
  const detailControllerRef = useRef(null)
  const mutationControllerRef = useRef(null)
  const listRequestRef = useRef(0)
  const detailRequestRef = useRef(0)

  const selectedAsset = selectedAssetDetail?.asset ?? null
  const selectedAssetId = assetId(selectedAsset)
  const selectedColumns = useMemo(() => {
    const rawColumns = selectedAssetDetail?.columns ?? selectedAsset?.columns ?? []
    return Array.isArray(rawColumns) ? rawColumns.filter(column => Boolean(columnId(column))) : []
  }, [selectedAsset, selectedAssetDetail])
  const scopeIsCurrent = selectedAssetDetail?.scope_matches_current_selection !== false
  const canEdit = Boolean(snapshotId && selectedAssetId && scopeIsCurrent && selectedColumns.length)
  const draftContextMatches = !editorOpen || Boolean(
    draftContext
    && draftContext.sourceId === sourceId
    && draftContext.snapshotId === snapshotId
    && draftContext.assetId === selectedAssetId,
  )
  const aggregateColumns = useMemo(
    () => eligibleAggregateColumns(selectedColumns, form.operation),
    [form.operation, selectedColumns],
  )
  const temporalColumns = useMemo(() => selectedColumns.filter(isTemporal), [selectedColumns])

  const resetEditor = useCallback(() => {
    setEditorOpen(false)
    setEditingMetric(null)
    setDraftContext(null)
    setForm(EMPTY_FORM)
    setFilters([])
    setConfirmed(false)
    setError(null)
  }, [])

  const loadMetrics = useCallback(async () => {
    listControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = listRequestRef.current + 1
    listRequestRef.current = requestId
    listControllerRef.current = controller
    setLoading(true)
    try {
      const response = await sourceApi.listMetricDefinitions(sourceId, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current || requestId !== listRequestRef.current) return
      const items = metricItems(response)
      setMetrics(items)
      setError(null)
      return items
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current && requestId === listRequestRef.current) {
        setError(safeErrorMessage(requestError, 'We could not load confirmed metric definitions.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestId === listRequestRef.current) setLoading(false)
    }
  }, [sourceId])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      listRequestRef.current += 1
      detailRequestRef.current += 1
      listControllerRef.current?.abort()
      detailControllerRef.current?.abort()
      mutationControllerRef.current?.abort()
    }
  }, [])

  const inspectMetric = useCallback(async metricId => {
    if (!metricId || inspecting) return
    detailControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = detailRequestRef.current + 1
    detailRequestRef.current = requestId
    detailControllerRef.current = controller
    setInspecting(true)
    setError(null)
    try {
      const [metricResponse, versionsResponse] = await Promise.all([
        sourceApi.getMetricDefinition(sourceId, metricId, { signal: controller.signal }),
        sourceApi.listMetricDefinitionVersions(sourceId, metricId, { signal: controller.signal }),
      ])
      if (controller.signal.aborted || !mountedRef.current || requestId !== detailRequestRef.current) return
      setSelectedMetric(metricFromResponse(metricResponse))
      setVersions(metricVersions(versionsResponse))
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current && requestId === detailRequestRef.current) {
        setError(safeErrorMessage(requestError, 'We could not load that metric definition.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestId === detailRequestRef.current) setInspecting(false)
    }
  }, [inspecting, sourceId])

  const updateForm = (field, value) => {
    setForm(current => {
      const next = { ...current, [field]: value }
      if (field === 'operation' && value === 'count') next.aggregateColumnId = ''
      if (field === 'defaultDateColumnId' && !value) next.defaultTimeZone = ''
      return next
    })
    setConfirmed(false)
    setError(null)
  }

  const updateFilter = (index, patch) => {
    setFilters(current => current.map((filter, filterIndex) => {
      if (filterIndex !== index) return filter
      const next = { ...filter, ...patch }
      const column = selectedColumns.find(item => String(columnId(item)) === String(next.column_id))
      const allowed = filterOperators(column)
      if (!allowed.some(item => item.value === next.operator)) next.operator = allowed[0]?.value ?? 'eq'
      if (isNullOperator(next.operator)) next.value = ''
      if (valueKind(column) === 'boolean' && next.value !== 'true' && next.value !== 'false') next.value = 'true'
      return next
    }))
    setConfirmed(false)
    setError(null)
  }

  const addFilter = () => {
    const column = selectedColumns[0]
    if (!column) return
    setFilters(current => [...current, {
      column_id: String(columnId(column)),
      operator: filterOperators(column)[0]?.value ?? 'eq',
      value: valueKind(column) === 'boolean' ? 'true' : '',
    }])
    setConfirmed(false)
  }

  const startCreate = () => {
    if (!canEdit) return
    resetEditor()
    setDraftContext({ sourceId, snapshotId, assetId: selectedAssetId })
    setEditorOpen(true)
  }

  const startEdit = () => {
    const version = currentVersion(selectedMetric, versions)
    if (!selectedMetric || !version) return
    const currentCatalog = selectedMetric.current_catalog
    if (!canEdit) {
      setError('Select this metric’s current authorized table above before creating a new version.')
      return
    }
    // A definition marked needs-review has no safe live-column projection by
    // design. Let its owner start a fresh confirmation against the selected
    // current table, but do not carry stale column IDs or fixed predicates
    // into that draft. The server performs the authoritative compatibility
    // check before it activates the new immutable version.
    if (selectedMetric.state === 'needs_review') {
      const aggregation = version.aggregation || {}
      setForm({
        name: selectedMetric.name ?? selectedMetric.display_name ?? '',
        description: selectedMetric.description ?? '',
        operation: aggregation.operation ?? 'count',
        aggregateColumnId: '',
        unitLabel: version.unit_label ?? '',
        defaultDateColumnId: '',
        defaultTimeZone: '',
      })
      setFilters([])
      setEditingMetric(selectedMetric)
      setDraftContext({ sourceId, snapshotId, assetId: selectedAssetId })
      setConfirmed(false)
      setEditorOpen(true)
      setError(null)
      setNotice('This metric needs review. Re-select its current columns and any fixed filters, then explicitly confirm a new immutable version.')
      return
    }
    // Immutable version IDs are deliberately snapshot-scoped. A compatible
    // refresh rotates them, so edit drafts must use the separate live catalog
    // projection returned by the server rather than comparing historical IDs
    // with the table currently selected in the inspector.
    if (
      !currentCatalog
      || String(currentCatalog.snapshot_id) !== String(snapshotId)
      || String(currentCatalog.asset_id) !== String(selectedAssetId)
    ) {
      setError('Select this metric’s current authorized table above before creating a new version.')
      return
    }
    const aggregation = version.aggregation || {}
    setForm({
      name: selectedMetric.name ?? selectedMetric.display_name ?? '',
      description: selectedMetric.description ?? '',
      operation: aggregation.operation ?? 'count',
      aggregateColumnId: currentCatalog.aggregation_column_id ?? '',
      unitLabel: version.unit_label ?? '',
      defaultDateColumnId: currentCatalog.default_date_column_id ?? '',
      defaultTimeZone: currentCatalog.default_time_zone ?? version.default_time_zone ?? '',
    })
    setFilters((currentCatalog.fixed_filters || []).map(filter => ({
      column_id: String(filter.column_id),
      operator: filter.operator,
      value: filterValueForForm(filter),
    })))
    setEditingMetric(selectedMetric)
    setDraftContext({ sourceId, snapshotId, assetId: selectedAssetId })
    setConfirmed(false)
    setEditorOpen(true)
    setError(null)
    setNotice('Saving this edit creates a new immutable version. The prior confirmed version remains in its history.')
  }

  const requestPayload = () => {
    const name = form.name.trim()
    if (!name) return { error: 'Enter a human-readable metric name before confirming.' }
    if (!canEdit) return { error: 'Select a current authorized table with column metadata before confirming.' }
    if (!draftContextMatches) {
      return { error: 'The selected snapshot or table changed. Start a new confirmed definition for the current table.' }
    }
    const aggregation = { operation: form.operation }
    if (aggregateNeedsColumn(form.operation)) {
      const aggregateColumn = aggregateColumns.find(column => String(columnId(column)) === String(form.aggregateColumnId))
      if (!aggregateColumn) return { error: 'Choose a compatible aggregate column for this operation.' }
      aggregation.column_id = columnId(aggregateColumn)
    }
    const fixedFilters = []
    for (const filter of filters) {
      const column = selectedColumns.find(item => String(columnId(item)) === String(filter.column_id))
      if (!column) return { error: 'Every fixed filter must use a column in the selected current table.' }
      const allowed = filterOperators(column)
      if (!allowed.some(item => item.value === filter.operator)) return { error: 'A fixed filter uses an unsupported operator for its column type.' }
      const next = { column_id: columnId(column), operator: filter.operator }
      if (!isNullOperator(filter.operator)) {
        const typed = typedValue(column, filter.value)
        if (typed.error) return { error: typed.error }
        next.value = typed.value
      }
      fixedFilters.push(next)
    }
    const defaultDateColumn = form.defaultDateColumnId
      ? temporalColumns.find(column => String(columnId(column)) === String(form.defaultDateColumnId))
      : null
    if (form.defaultDateColumnId && !defaultDateColumn) return { error: 'Choose a compatible date or timestamp column for the default time basis.' }
    if (form.defaultTimeZone.trim() && !defaultDateColumn) return { error: 'Choose a default date column before setting a time zone.' }
    return {
      value: {
        name,
        // A visible owner action is required for every create/revision. This
        // is not inferred from the model, a table comment, or the chat text.
        owner_confirmed: true,
        ...(form.description.trim() ? { description: form.description.trim() } : {}),
        snapshot_id: snapshotId,
        asset_id: selectedAssetId,
        aggregation,
        ...(fixedFilters.length ? { fixed_filters: fixedFilters } : {}),
        ...(form.unitLabel.trim() ? { unit_label: form.unitLabel.trim() } : {}),
        ...(defaultDateColumn ? { default_date_column_id: columnId(defaultDateColumn) } : {}),
        ...(form.defaultTimeZone.trim() ? { default_time_zone: form.defaultTimeZone.trim() } : {}),
      },
    }
  }

  const saveDefinition = async event => {
    event.preventDefault()
    if (saving) return
    if (!confirmed) {
      setError('Review the plain-language definition and explicitly confirm it before saving.')
      return
    }
    const payload = requestPayload()
    if (payload.error) {
      setError(payload.error)
      return
    }
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setSaving(true)
    setError(null)
    try {
      const response = editingMetric
        ? await sourceApi.createMetricDefinitionVersion(sourceId, editingMetric.metric_id, payload.value, { signal: controller.signal })
        : await sourceApi.createMetricDefinition(sourceId, payload.value, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      const savedMetric = metricFromResponse(response)
      setSelectedMetric(savedMetric)
      setVersions(currentVersion(savedMetric) ? [currentVersion(savedMetric)] : [])
      setNotice(editingMetric
        ? 'A new immutable metric version was confirmed.'
        : 'Metric definition confirmed. It is available only when the current catalog remains compatible.')
      resetEditor()
      // The mutation response preserves immutable version provenance, while
      // the refreshed list includes the current_catalog projection. Replace
      // the selected card with that live projection so an owner can revise a
      // just-confirmed metric again without first reopening its details.
      const refreshedMetrics = await loadMetrics()
      const refreshedMetric = Array.isArray(refreshedMetrics)
        ? refreshedMetrics.find(item => String(item.metric_id) === String(savedMetric?.metric_id))
        : null
      if (!controller.signal.aborted && mountedRef.current && refreshedMetric) {
        setSelectedMetric(refreshedMetric)
      }
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(safeErrorMessage(requestError, 'We could not save this metric definition. Your draft was not discarded.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setSaving(false)
    }
  }

  const retireDefinition = async () => {
    if (!retireTarget || retiring) return
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setRetiring(true)
    setError(null)
    try {
      await sourceApi.retireMetricDefinition(sourceId, retireTarget.metric_id, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      setNotice('Metric definition retired. Historical execution provenance remains unchanged.')
      setRetireTarget(null)
      setSelectedMetric(null)
      setVersions([])
      await loadMetrics()
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(safeErrorMessage(requestError, 'We could not retire this metric definition.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setRetiring(false)
    }
  }

  const preview = definitionSummary({
    asset: selectedAsset,
    operation: form.operation,
    aggregateColumnId: form.aggregateColumnId,
    filters,
    columns: selectedColumns,
    unitLabel: form.unitLabel,
    defaultDateColumnId: form.defaultDateColumnId,
    defaultTimeZone: form.defaultTimeZone,
  })
  const selectedVersion = currentVersion(selectedMetric, versions)
  const selectedTimeBasis = [
    selectedVersion?.default_date_column_name ?? selectedVersion?.default_date_column_id,
    selectedVersion?.default_time_zone,
  ].filter(Boolean).join(' · ') || '—'
  const toggleOpen = () => {
    if (open) {
      setOpen(false)
      return
    }
    setOpen(true)
    void loadMetrics()
  }

  return (
    <section className="metric-definitions" aria-labelledby="metric-definitions-heading">
      <div className="catalog-browser-heading metric-definitions-heading">
        <div>
          <h4 id="metric-definitions-heading">Metric definitions</h4>
          <p>Owner-confirmed, reusable single-table calculations. Database comments are not metric definitions.</p>
        </div>
        <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={toggleOpen}>
          {open ? 'Hide metric definitions' : 'Manage metric definitions'}
        </button>
      </div>

      {!open ? <p className="catalog-definition-note">Definitions are confirmed explicitly here; database chat never promotes a question or clarification into a saved metric.</p> : (
        <>
          {error && <p className="source-alert" role="alert">{error}</p>}
          {notice && <p className="source-notice" role="status">{notice}</p>}
          {!snapshotId && <p className="source-guidance" role="note">A current published metadata snapshot is required to create or revise a metric. Existing definitions can still be inspected or retired.</p>}
          {snapshotId && !selectedAssetDetail && <p className="source-guidance" role="note">Select a discovered table above to create or revise a metric definition.</p>}
          {selectedAssetDetail && !scopeIsCurrent && <p className="source-guidance" role="note">The selected table is no longer fully authorized by the current scope. Refresh metadata before defining a metric.</p>}
          {selectedAssetDetail && scopeIsCurrent && selectedColumns.length === 0 && <p className="source-guidance" role="note">The selected table did not provide stable column references for a metric definition. Refresh metadata before continuing.</p>}

          <div className="metric-definitions-actions source-actions">
            <button className="btn btn-primary btn-sm" type="button" onClick={startCreate} disabled={!canEdit || saving}>
              New metric definition
            </button>
            <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => void loadMetrics()} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh definitions'}
            </button>
          </div>

          {loading && metrics.length === 0 ? <p className="source-empty" role="status">Loading confirmed metric definitions…</p> : metrics.length === 0 ? (
            <p className="source-empty">No metric definitions have been confirmed for this source.</p>
          ) : (
            <div className="metric-definition-list">
              {metrics.map(metric => <MetricCard key={metric.metric_id} metric={metric} onInspect={inspectMetric} loading={inspecting} />)}
            </div>
          )}

          {selectedMetric && (
            <section className="metric-definition-detail" aria-labelledby="metric-definition-detail-heading">
              <div className="metric-definition-card-heading">
                <div>
                  <h5 id="metric-definition-detail-heading">{selectedMetric.name ?? selectedMetric.display_name}</h5>
                  <p>{displayState(selectedMetric.state)} · Version {selectedVersion?.version_number ?? '—'}</p>
                </div>
                <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => { setSelectedMetric(null); setVersions([]) }}>Close details</button>
              </div>
              {selectedMetric.description && <p className="metric-definition-description">{selectedMetric.description}</p>}
              <dl className="metric-definition-summary">
                <div><dt>Aggregate</dt><dd>{displayOperation(selectedVersion?.aggregation?.operation)}</dd></div>
                <div><dt>Unit</dt><dd>{selectedVersion?.unit_label || '—'}</dd></div>
                <div><dt>Default time basis</dt><dd>{selectedTimeBasis}</dd></div>
              </dl>
              <p className="metric-definition-provenance">Confirmed {dateText(selectedVersion?.confirmed_at ?? selectedMetric.confirmed_at)}. Fixed filters and references are validated again by the server before use.</p>
              {versions.length > 1 && (
                <ol className="metric-version-list" aria-label="Metric definition versions">
                  {[...versions].sort((left, right) => (right.version_number ?? 0) - (left.version_number ?? 0)).map(version => (
                    <li key={version.version_number}>Version {version.version_number} · confirmed {dateText(version.confirmed_at)}</li>
                  ))}
                </ol>
              )}
              <div className="source-actions">
                <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={startEdit} disabled={selectedMetric.state === 'retired' || saving}>Create revised version</button>
                <button className="btn btn-ghost-neutral btn-sm source-delete-button" type="button" onClick={() => setRetireTarget(selectedMetric)} disabled={selectedMetric.state === 'retired' || retiring}>Retire definition</button>
              </div>
            </section>
          )}

          {editorOpen && (
            <form className="metric-definition-editor" onSubmit={saveDefinition}>
              <div className="metric-definition-editor-heading">
                <div>
                  <h5>{editingMetric ? 'Create revised metric version' : 'Confirm a metric definition'}</h5>
                  <p>{assetLabel(selectedAsset)} from the current published metadata snapshot.</p>
                </div>
                <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={resetEditor} disabled={saving}>Cancel</button>
              </div>
              {!draftContextMatches && <p className="source-guidance" role="note">The selected snapshot or table changed after this draft began. Cancel and start a new confirmed definition for the current table.</p>}
              <div className="source-form-grid metric-form-grid">
                <label className="source-field source-field-wide">
                  <span>Metric name</span>
                  <input value={form.name} onChange={event => updateForm('name', event.target.value)} maxLength={128} disabled={saving} autoComplete="off" placeholder="e.g. Paid order count" />
                </label>
                <label className="source-field source-field-wide">
                  <span>Description (optional)</span>
                  <textarea value={form.description} onChange={event => updateForm('description', event.target.value)} maxLength={2000} disabled={saving} rows={2} placeholder="Explain the confirmed business meaning in plain language" />
                </label>
                <label className="source-field">
                  <span>Aggregate</span>
                  <select value={form.operation} onChange={event => updateForm('operation', event.target.value)} disabled={saving}>
                    {AGGREGATES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
                  </select>
                </label>
                {aggregateNeedsColumn(form.operation) ? (
                  <label className="source-field">
                    <span>Aggregate column</span>
                    <select value={form.aggregateColumnId} onChange={event => updateForm('aggregateColumnId', event.target.value)} disabled={saving}>
                      <option value="">Choose a compatible column</option>
                      {aggregateColumns.map(column => <option key={columnId(column)} value={columnId(column)}>{quoteIdentifier(columnName(column))} · {column.formatted_native_type || nativeType(column)}</option>)}
                    </select>
                  </label>
                ) : <div className="source-fixed-field"><span>Aggregate column</span><strong>All rows</strong></div>}
                <label className="source-field">
                  <span>Unit label (optional)</span>
                  <input value={form.unitLabel} onChange={event => updateForm('unitLabel', event.target.value)} maxLength={64} disabled={saving} autoComplete="off" placeholder="e.g. orders" />
                </label>
                <label className="source-field">
                  <span>Default date column (optional)</span>
                  <select value={form.defaultDateColumnId} onChange={event => updateForm('defaultDateColumnId', event.target.value)} disabled={saving}>
                    <option value="">No default time basis</option>
                    {temporalColumns.map(column => <option key={columnId(column)} value={columnId(column)}>{quoteIdentifier(columnName(column))} · {column.formatted_native_type || nativeType(column)}</option>)}
                  </select>
                </label>
                <label className="source-field">
                  <span>Default time zone (optional)</span>
                  <input value={form.defaultTimeZone} onChange={event => updateForm('defaultTimeZone', event.target.value)} maxLength={128} disabled={saving || !form.defaultDateColumnId} autoComplete="off" placeholder="e.g. UTC" />
                </label>
              </div>

              <section className="metric-filter-editor" aria-labelledby="metric-fixed-filters-heading">
                <div className="metric-definition-editor-heading">
                  <div>
                    <h6 id="metric-fixed-filters-heading">Fixed row filters</h6>
                    <p>These constraints are part of the confirmed definition. A later question can add filters but cannot remove them.</p>
                  </div>
                  <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={addFilter} disabled={saving || selectedColumns.length === 0}>Add fixed filter</button>
                </div>
                {filters.length === 0 ? <p className="source-empty">No fixed row filters.</p> : (
                  <div className="metric-filter-list">
                    {filters.map((filter, index) => {
                      const column = selectedColumns.find(item => String(columnId(item)) === String(filter.column_id)) || selectedColumns[0]
                      const operators = filterOperators(column)
                      const kind = valueKind(column)
                      return (
                        <div className="metric-filter-row" key={`${filter.column_id}-${index}`}>
                          <label className="source-field">
                            <span className="sr-only">Filter column {index + 1}</span>
                            <select value={filter.column_id} onChange={event => updateFilter(index, { column_id: event.target.value })} disabled={saving}>
                              {selectedColumns.map(item => <option key={columnId(item)} value={columnId(item)}>{quoteIdentifier(columnName(item))}</option>)}
                            </select>
                          </label>
                          <label className="source-field">
                            <span className="sr-only">Filter operator {index + 1}</span>
                            <select value={filter.operator} onChange={event => updateFilter(index, { operator: event.target.value })} disabled={saving}>
                              {operators.map(operator => <option key={operator.value} value={operator.value}>{operator.label}</option>)}
                            </select>
                          </label>
                          {!isNullOperator(filter.operator) && (
                            <label className="source-field">
                              <span className="sr-only">Filter value {index + 1}</span>
                              {kind === 'boolean' ? (
                                <select value={filter.value} onChange={event => updateFilter(index, { value: event.target.value })} disabled={saving}>
                                  <option value="true">true</option><option value="false">false</option>
                                </select>
                              ) : <input value={filter.value} onChange={event => updateFilter(index, { value: event.target.value })} disabled={saving} autoComplete="off" placeholder={`Value (${kind})`} />}
                            </label>
                          )}
                          <button className="btn btn-ghost-neutral btn-sm source-delete-button" type="button" onClick={() => { setFilters(current => current.filter((_, filterIndex) => filterIndex !== index)); setConfirmed(false) }} disabled={saving}>Remove</button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>

              <section className="metric-definition-preview" aria-labelledby="metric-definition-preview-heading">
                <h6 id="metric-definition-preview-heading">Review before confirming</h6>
                <p>{preview}</p>
                <label className="metric-confirmation">
                  <input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={saving} />
                  <span>I confirm this is the intended reusable business definition for this authorized table.</span>
                </label>
              </section>
              <div className="source-actions">
                <button className="btn btn-primary" type="submit" disabled={!confirmed || saving || !draftContextMatches}>{saving ? 'Saving confirmed definition…' : editingMetric ? 'Confirm new version' : 'Save confirmed metric'}</button>
              </div>
            </form>
          )}

          {retireTarget && (
            <div className="source-delete-confirmation metric-retire-confirmation" role="alertdialog" aria-modal="false" aria-labelledby="retire-metric-title">
              <div>
                <h5 id="retire-metric-title">Retire “{retireTarget.name ?? retireTarget.display_name}”?</h5>
                <p>This prevents new database-chat use. It does not alter historical execution provenance or existing versions.</p>
              </div>
              <div className="source-actions">
                <button className="btn btn-ghost-neutral" type="button" onClick={() => setRetireTarget(null)} disabled={retiring}>Cancel</button>
                <button className="btn btn-primary source-delete-button" type="button" onClick={() => void retireDefinition()} disabled={retiring}>{retiring ? 'Retiring…' : 'Retire definition'}</button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  )
}
