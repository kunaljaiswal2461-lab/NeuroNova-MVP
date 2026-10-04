import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { sourceApi, sourceErrorCode } from '../../api/sources'
import CatalogInspector from './CatalogInspector'
import SavedSourceMaintenance from './SavedSourceMaintenance'

const EMPTY_FORM = {
  displayName: '',
  host: '',
  databaseName: '',
  username: '',
  password: '',
  customCaPem: '',
}

const CONNECTION_FIELDS = new Set(['host', 'databaseName', 'username', 'password', 'customCaPem'])

const ERROR_MESSAGES = {
  authentication_required: 'Your sign-in session is unavailable. Please sign in again to manage database connections.',
  connectors_disabled: 'Database connections are currently unavailable. Please ask an administrator to enable them.',
  connector_egress_not_configured: 'Database connections require administrator setup before they can be used.',
  credential_key_unavailable: 'Database connections require administrator setup before they can be used.',
  unsupported_connection_settings: 'Use a public DNS hostname, PostgreSQL port 5432, and verified TLS.',
  connection_failed: 'We could not connect. Check the host, database, credentials, and TLS setup, then try again.',
  connection_timeout: 'The database did not respond in time. Verify that it is reachable and try again.',
  tls_verification_failed: 'The database TLS certificate could not be verified. Check the hostname and certificate authority.',
  inventory_limit_exceeded: 'This role exposes more than 1,000 eligible tables. Restrict its access before onboarding.',
  no_eligible_tables: 'No eligible base tables were found for this role. Check its schema usage and table-level read access.',
  invalid_source_scope: 'The eligible table scope changed. Test the connection again before saving.',
}

function copyScope(table) {
  return { schema_name: table.schema_name, table_name: table.table_name }
}

function scopeKey(table) {
  // Structural tuple serialization avoids treating a display separator as an
  // identifier boundary. Selection itself stores explicit scope objects.
  return JSON.stringify([table.schema_name, table.table_name])
}

function quoteIdentifier(identifier) {
  // PostgreSQL identifiers can themselves contain dots and double quotes.
  // Quote each tuple component for display so valid pairs cannot collapse
  // into the same visible or accessible name.
  return `"${identifier.replace(/"/g, '""')}"`
}

function tableLabel(table) {
  return `${quoteIdentifier(table.schema_name)}.${quoteIdentifier(table.table_name)}`
}

function sameScope(left, right) {
  return left.schema_name === right.schema_name && left.table_name === right.table_name
}

function hasScope(scopes, table) {
  return scopes.some(scope => sameScope(scope, table))
}

function sourceErrorMessage(error) {
  return ERROR_MESSAGES[sourceErrorCode(error)] || 'We could not complete that database request. Please try again.'
}

function connectionRequest(form) {
  const customCaPem = form.customCaPem.trim()
  return {
    host: form.host.trim(),
    port: 5432,
    database_name: form.databaseName.trim(),
    tls: {
      mode: 'verify-full',
      ...(customCaPem ? { custom_ca_pem: customCaPem } : {}),
    },
    credentials: {
      username: form.username,
      password: form.password,
    },
  }
}

function formIsComplete(form) {
  return Boolean(form.host.trim() && form.databaseName.trim() && form.username && form.password)
}

function dateText(value) {
  if (!value) return 'No test time recorded'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Test time unavailable' : date.toLocaleString()
}

function sourceStatusLabel(status) {
  if (status === 'TESTED') return 'Previously tested'
  if (status === 'CONNECTION_FAILED') return 'Connection needs retesting'
  if (status === 'CREDENTIAL_UNAVAILABLE') return 'Credentials unavailable'
  // Never turn an unexpected persisted value into a premature catalog/query
  // readiness claim in the Stage 2 onboarding UI.
  return 'Status unavailable'
}

function sourceStatusText(source) {
  if (source.status === 'TESTED') {
    return `Saved after a successful test · ${dateText(source.last_tested_at)}`
  }
  if (source.status === 'CONNECTION_FAILED') return 'This saved connection needs retesting.'
  if (source.status === 'CREDENTIAL_UNAVAILABLE') return 'Credentials need administrator attention before a later-stage use.'
  return 'Saved connection status is unavailable.'
}

function SchemaSelectionCheckbox({ schemaName, tables, selectedTables, onChange, disabled }) {
  const checkboxRef = useRef(null)
  const selectedCount = tables.filter(table => hasScope(selectedTables, table)).length
  const allSelected = tables.length > 0 && selectedCount === tables.length
  const partial = selectedCount > 0 && !allSelected

  useEffect(() => {
    if (checkboxRef.current) checkboxRef.current.indeterminate = partial
  }, [partial])

  return (
    <label className="source-schema-toggle">
      <input
        ref={checkboxRef}
        type="checkbox"
        checked={allSelected}
        aria-label={`${schemaName} schema selection`}
        aria-checked={partial ? 'mixed' : allSelected}
        onChange={event => onChange(event.target.checked)}
        disabled={disabled}
      />
      <span>{schemaName}</span>
      <span className="source-count">{selectedCount}/{tables.length}</span>
    </label>
  )
}

export default function DatabaseSourcesPanel({ active }) {
  const [form, setForm] = useState(EMPTY_FORM)
  const [showCustomCa, setShowCustomCa] = useState(false)
  const [inventory, setInventory] = useState(null)
  const [selectedTables, setSelectedTables] = useState([])
  const [tableFilter, setTableFilter] = useState('')
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [sources, setSources] = useState([])
  const [listLoading, setListLoading] = useState(false)
  const [listError, setListError] = useState(null)
  const [detailsById, setDetailsById] = useState({})
  const [detailLoadingId, setDetailLoadingId] = useState(null)
  const [detailErrorById, setDetailErrorById] = useState({})
  const [expandedSourceId, setExpandedSourceId] = useState(null)
  const [metadataSourceId, setMetadataSourceId] = useState(null)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [deleteError, setDeleteError] = useState(null)
  const [deletingId, setDeletingId] = useState(null)

  const mountedRef = useRef(false)
  const formRef = useRef(EMPTY_FORM)
  const testRequestRef = useRef(0)
  const listRequestRef = useRef(0)
  const testControllerRef = useRef(null)
  const saveControllerRef = useRef(null)
  const listControllerRef = useRef(null)
  const detailControllerRef = useRef(null)
  const deleteControllerRef = useRef(null)
  const saveInFlightRef = useRef(false)

  const clearTransientForm = useCallback(() => {
    formRef.current = EMPTY_FORM
    setForm(EMPTY_FORM)
    setShowCustomCa(false)
  }, [])

  const invalidateTest = useCallback(() => {
    testRequestRef.current += 1
    testControllerRef.current?.abort()
    testControllerRef.current = null
    setTesting(false)
    setInventory(null)
    setSelectedTables([])
    setTableFilter('')
    setFormError(null)
  }, [])

  const loadSources = useCallback(async () => {
    listControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = listRequestRef.current + 1
    listRequestRef.current = requestId
    listControllerRef.current = controller
    setListLoading(true)
    try {
      const result = await sourceApi.listSources({ signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current || requestId !== listRequestRef.current) return
      setSources(result.items || [])
      setListError(null)
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current && requestId === listRequestRef.current) {
        setListError(sourceErrorMessage(error))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestId === listRequestRef.current) {
        setListLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      testRequestRef.current += 1
      listRequestRef.current += 1
      testControllerRef.current?.abort()
      saveControllerRef.current?.abort()
      listControllerRef.current?.abort()
      detailControllerRef.current?.abort()
      deleteControllerRef.current?.abort()
      // Drop our direct credential reference on page exit. JavaScript cannot
      // guarantee memory erasure, but this component never persists it.
      formRef.current = EMPTY_FORM
    }
  }, [])

  useEffect(() => {
    if (active) loadSources()
  }, [active, loadSources])

  const schemas = useMemo(() => {
    const bySchema = new Map()
    for (const table of inventory || []) {
      const tables = bySchema.get(table.schema_name) || []
      tables.push(table)
      bySchema.set(table.schema_name, tables)
    }
    return [...bySchema.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([schemaName, tables]) => ({
        schemaName,
        tables: [...tables].sort((left, right) => left.table_name.localeCompare(right.table_name)),
      }))
  }, [inventory])

  const normalizedFilter = tableFilter.trim().toLocaleLowerCase()
  const visibleSchemas = useMemo(() => schemas.map(({ schemaName, tables }) => ({
    schemaName,
    allTables: tables,
    tables: normalizedFilter
      ? tables.filter(table => `${schemaName} ${table.table_name}`.toLocaleLowerCase().includes(normalizedFilter))
      : tables,
  })).filter(schema => schema.tables.length > 0), [normalizedFilter, schemas])

  const allCurrentTablesSelected = Boolean(inventory?.length) && inventory.every(table => hasScope(selectedTables, table))
  const metadataSource = useMemo(
    () => sources.find(source => source.source_id === metadataSourceId) || null,
    [metadataSourceId, sources],
  )

  function updateField(field, value) {
    if (formRef.current[field] === value) return
    const next = { ...formRef.current, [field]: value }
    formRef.current = next
    setForm(next)
    if (CONNECTION_FIELDS.has(field)) invalidateTest()
    setNotice(null)
  }

  function toggleTable(table, checked) {
    setSelectedTables(current => checked
      ? hasScope(current, table) ? current : [...current, copyScope(table)]
      : current.filter(scope => !sameScope(scope, table)))
  }

  function toggleSchema(tables, checked) {
    setSelectedTables(current => {
      if (!checked) return current.filter(scope => !tables.some(table => sameScope(scope, table)))
      return tables.reduce((next, table) => hasScope(next, table) ? next : [...next, copyScope(table)], current)
    })
  }

  function toggleAllCurrentTables() {
    setSelectedTables(allCurrentTablesSelected ? [] : (inventory || []).map(copyScope))
  }

  async function handleTest(event) {
    event.preventDefault()
    const currentForm = formRef.current
    if (!formIsComplete(currentForm)) {
      setFormError('Enter a host, database, username, and password before testing the connection.')
      return
    }
    testControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = testRequestRef.current + 1
    testRequestRef.current = requestId
    testControllerRef.current = controller
    setTesting(true)
    setFormError(null)
    setNotice(null)
    setInventory(null)
    setSelectedTables([])
    setTableFilter('')
    try {
      const result = await sourceApi.testPostgres(connectionRequest(currentForm), { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current || requestId !== testRequestRef.current) return
      const eligibleTables = Array.isArray(result.eligible_tables) ? result.eligible_tables.map(copyScope) : []
      setInventory(eligibleTables)
      if (eligibleTables.length === 0) {
        setFormError('No eligible base tables were returned for this connection.')
      }
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current && requestId === testRequestRef.current) {
        setFormError(sourceErrorMessage(error))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestId === testRequestRef.current) {
        setTesting(false)
      }
    }
  }

  async function handleSave() {
    const currentForm = formRef.current
    if (!inventory?.length) {
      setFormError('Test the connection and select at least one table before saving.')
      return
    }
    if (selectedTables.length === 0) {
      setFormError('Select at least one table before saving this connection.')
      return
    }
    if (!currentForm.displayName.trim()) {
      setFormError('Enter a connection name before saving.')
      return
    }
    if (saving || saveInFlightRef.current) return
    const controller = new AbortController()
    saveControllerRef.current = controller
    saveInFlightRef.current = true
    setSaving(true)
    setFormError(null)
    setNotice(null)
    try {
      const created = await sourceApi.createSource({
        ...connectionRequest(currentForm),
        display_name: currentForm.displayName.trim(),
        selected_tables: selectedTables.map(copyScope),
        profiling_enabled: false,
      }, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      setSources(current => [created, ...current.filter(source => source.source_id !== created.source_id)])
      setDetailsById(current => ({ ...current, [created.source_id]: created }))
      setExpandedSourceId(created.source_id)
      setInventory(null)
      setSelectedTables([])
      setTableFilter('')
      clearTransientForm()
      setNotice('Connection saved. Select View metadata to request structural metadata synchronization.')
      await loadSources()
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        if (sourceErrorCode(error) === 'invalid_source_scope') {
          setInventory(null)
          setSelectedTables([])
          setTableFilter('')
        }
        if (error?.transient || error?.name === 'TimeoutError' || error instanceof TypeError) {
          setFormError('We could not confirm whether the connection was saved. Refresh Saved connections before submitting again.')
        } else {
          setFormError(sourceErrorMessage(error))
        }
      }
    } finally {
      saveInFlightRef.current = false
      if (!controller.signal.aborted && mountedRef.current) setSaving(false)
    }
  }

  function handleCancel() {
    if (saving) return
    invalidateTest()
    clearTransientForm()
    setNotice(null)
  }

  async function showSourceDetails(sourceId) {
    if (expandedSourceId === sourceId) {
      setExpandedSourceId(null)
      return
    }
    setExpandedSourceId(sourceId)
    if (detailsById[sourceId]) return
    detailControllerRef.current?.abort()
    const controller = new AbortController()
    detailControllerRef.current = controller
    setDetailLoadingId(sourceId)
    setDetailErrorById(current => ({ ...current, [sourceId]: null }))
    try {
      const detail = await sourceApi.getSource(sourceId, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      setDetailsById(current => ({ ...current, [sourceId]: detail }))
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) {
        setDetailErrorById(current => ({ ...current, [sourceId]: sourceErrorMessage(error) }))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setDetailLoadingId(null)
    }
  }

  async function deleteSource() {
    if (!deleteTarget || deletingId) return
    const controller = new AbortController()
    deleteControllerRef.current = controller
    setDeletingId(deleteTarget.source_id)
    setDeleteError(null)
    try {
      await sourceApi.deleteSource(deleteTarget.source_id, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      setSources(current => current.filter(source => source.source_id !== deleteTarget.source_id))
      setExpandedSourceId(current => current === deleteTarget.source_id ? null : current)
      setMetadataSourceId(current => current === deleteTarget.source_id ? null : current)
      setDetailsById(current => {
        const next = { ...current }
        delete next[deleteTarget.source_id]
        return next
      })
      setDeleteTarget(null)
      await loadSources()
    } catch (error) {
      if (!controller.signal.aborted && mountedRef.current) setDeleteError(sourceErrorMessage(error))
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setDeletingId(null)
    }
  }

  return (
    <div className="database-sources-panel">
      <div className="source-intro">
        <div>
          <h2>Connect PostgreSQL</h2>
          <p>Add a narrowly scoped PostgreSQL connection without importing client tables into NeuroNova.</p>
        </div>
        <span className="source-stage-chip">PostgreSQL only</span>
      </div>

      <div className="source-guidance" role="note">
        <strong>Supported in this stage:</strong> public DNS hostname, port 5432, and verified TLS. A custom CA is optional. Credentials stay in this form only while you test and save the connection.
      </div>

      <form className="source-card source-form" onSubmit={handleTest}>
        <div className="source-card-heading">
          <div>
            <h3>Connection details</h3>
            <p>Use a dedicated read-only database role. Profiling is not enabled in this stage.</p>
          </div>
          <span className="source-tls-chip">TLS · verify-full</span>
        </div>

        <div className="source-form-grid">
          <label className="source-field source-field-wide">
            <span>Connection name</span>
            <input
              type="text"
              value={form.displayName}
              onChange={event => updateField('displayName', event.target.value)}
              autoComplete="off"
              maxLength={128}
              disabled={saving}
              placeholder="e.g. Production analytics"
            />
          </label>
          <label className="source-field">
            <span>Public DNS hostname</span>
            <input
              type="text"
              value={form.host}
              onChange={event => updateField('host', event.target.value)}
              autoComplete="off"
              required
              disabled={saving}
              placeholder="db.example.com"
            />
          </label>
          <label className="source-field">
            <span>Database name</span>
            <input
              type="text"
              value={form.databaseName}
              onChange={event => updateField('databaseName', event.target.value)}
              autoComplete="off"
              required
              disabled={saving}
              placeholder="analytics"
            />
          </label>
          <label className="source-field">
            <span>Username</span>
            <input
              type="text"
              value={form.username}
              onChange={event => updateField('username', event.target.value)}
              autoComplete="off"
              required
              disabled={saving}
            />
          </label>
          <label className="source-field">
            <span>Password</span>
            <input
              type="password"
              value={form.password}
              onChange={event => updateField('password', event.target.value)}
              autoComplete="off"
              required
              disabled={saving}
            />
          </label>
          <div className="source-fixed-field" aria-label="PostgreSQL port">
            <span>PostgreSQL port</span>
            <strong>5432</strong>
          </div>
        </div>

        <label className="source-ca-toggle">
          <input
            type="checkbox"
            checked={showCustomCa}
            disabled={saving}
            onChange={event => {
              setShowCustomCa(event.target.checked)
              if (!event.target.checked) updateField('customCaPem', '')
            }}
          />
          <span>Use a custom certificate authority (optional)</span>
        </label>
        {showCustomCa && (
          <label className="source-field source-ca-field">
            <span>Custom CA PEM</span>
            <textarea
              value={form.customCaPem}
              onChange={event => updateField('customCaPem', event.target.value)}
              autoComplete="off"
              disabled={saving}
              rows={5}
              placeholder="-----BEGIN CERTIFICATE-----"
            />
          </label>
        )}

        {formError && <p className="source-alert" role="alert">{formError}</p>}
        {notice && <p className="source-notice" role="status">{notice}</p>}

        <div className="source-actions">
          <button className="btn btn-primary" type="submit" disabled={testing || saving}>
            {testing ? 'Testing connection…' : 'Test connection'}
          </button>
          <button className="btn btn-ghost-neutral" type="button" onClick={handleCancel} disabled={saving}>
            Clear connection details
          </button>
        </div>
      </form>

      {inventory && (
        <section className="source-card source-inventory" aria-labelledby="eligible-tables-heading">
          <div className="source-card-heading">
            <div>
              <h3 id="eligible-tables-heading">Eligible tables</h3>
              <p>Choose the tables this source may include. Every table starts excluded.</p>
            </div>
            <span className="source-count">{selectedTables.length} selected</span>
          </div>

          {inventory.length > 0 ? (
            <>
              <div className="source-inventory-actions">
                <label className="source-search">
                  <span className="sr-only">Filter eligible tables</span>
                  <input
                    type="search"
                    value={tableFilter}
                    onChange={event => setTableFilter(event.target.value)}
                    placeholder="Filter schemas or tables"
                    disabled={saving}
                  />
                </label>
                <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={toggleAllCurrentTables} disabled={saving}>
                  {allCurrentTablesSelected ? 'Clear all current tables' : `Select all current tables (${inventory.length})`}
                </button>
              </div>
              <p className="source-selection-help">Search changes what is displayed only. Schema and select-all controls affect every table returned by this test, not just filtered results. Future tables stay excluded until selected in a later refresh.</p>

              <div className="source-schema-list">
                {visibleSchemas.length === 0 ? (
                  <p className="source-empty">No eligible tables match that filter.</p>
                ) : visibleSchemas.map(({ schemaName, allTables, tables }) => (
                  <div className="source-schema" key={schemaName}>
                    <SchemaSelectionCheckbox
                      schemaName={schemaName}
                      tables={allTables}
                      selectedTables={selectedTables}
                      onChange={checked => toggleSchema(allTables, checked)}
                      disabled={saving}
                    />
                    <div className="source-table-list">
                      {tables.map(table => {
                        const checked = hasScope(selectedTables, table)
                        const label = tableLabel(table)
                        return (
                          <label className="source-table-toggle" key={scopeKey(table)}>
                            <input type="checkbox" checked={checked} onChange={event => toggleTable(table, event.target.checked)} disabled={saving} />
                            <span>{label}</span>
                          </label>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
              <div className="source-save-row">
                <span>{selectedTables.length} table{selectedTables.length === 1 ? '' : 's'} selected</span>
                <button className="btn btn-primary" type="button" onClick={handleSave} disabled={saving || testing || selectedTables.length === 0}>
                  {saving ? 'Saving connection…' : 'Save selected connection'}
                </button>
              </div>
            </>
          ) : (
            <p className="source-empty">No eligible tables are available for this connection.</p>
          )}
        </section>
      )}

      <section className="source-card source-saved" aria-labelledby="saved-sources-heading">
        <div className="source-card-heading">
          <div>
            <h3 id="saved-sources-heading">Saved connections</h3>
            <p>These are your NeuroNova connection configurations, not copies of client tables or records.</p>
          </div>
          <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={loadSources} disabled={listLoading}>
            {listLoading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
        {listError && <p className="source-alert" role="alert">{listError}</p>}
        {listLoading && sources.length === 0 ? (
          <p className="source-empty" role="status">Loading saved connections…</p>
        ) : sources.length === 0 ? (
          <p className="source-empty">No saved PostgreSQL connections yet.</p>
        ) : (
          <div className="source-saved-list">
            {sources.map(source => {
              const detail = detailsById[source.source_id]
              const expanded = expandedSourceId === source.source_id
              return (
                <article className="source-saved-item" key={source.source_id}>
                  <div className="source-saved-summary">
                    <div>
                      <h4>{source.display_name}</h4>
                      <p>{source.host}:{source.port} · {source.database_name}</p>
                    </div>
                    <span className="source-status-chip">{sourceStatusLabel(source.status)}</span>
                  </div>
                  <dl className="source-summary-fields">
                    <div><dt>Scope</dt><dd>{source.scope_count} table{source.scope_count === 1 ? '' : 's'}</dd></div>
                    <div><dt>TLS</dt><dd>{source.tls_mode === 'VERIFY_FULL' ? 'Verified TLS' : source.tls_mode}</dd></div>
                    <div><dt>Custom CA</dt><dd>{source.has_custom_ca ? 'Configured' : 'Not configured'}</dd></div>
                  </dl>
                  <p className="source-previous-test">{sourceStatusText(source)}</p>
                  <div className="source-saved-actions">
                    <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => showSourceDetails(source.source_id)}>
                      {expanded ? 'Hide selected tables' : 'View selected tables'}
                    </button>
                    <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => setMetadataSourceId(source.source_id)}>
                      View metadata
                    </button>
                    <button className="btn btn-ghost-neutral btn-sm source-delete-button" type="button" onClick={() => { setDeleteTarget(source); setDeleteError(null) }}>
                      Delete connection
                    </button>
                  </div>
                  {expanded && (
                    <div className="source-detail" aria-live="polite">
                      {detailLoadingId === source.source_id && <p>Loading selected tables…</p>}
                      {detailErrorById[source.source_id] && <p className="source-alert" role="alert">{detailErrorById[source.source_id]}</p>}
                      {detail && (
                        <>
                          <h5>Selected table scope</h5>
                          <ul>
                            {detail.selected_tables.map(table => <li key={scopeKey(table)}>{tableLabel(table)}</li>)}
                          </ul>
                          <SavedSourceMaintenance
                            source={source}
                            detail={detail}
                            onUpdated={updated => {
                              if (!updated?.source_id) return
                              setSources(current => current.map(item => item.source_id === updated.source_id ? { ...item, ...updated } : item))
                              setDetailsById(current => ({ ...current, [updated.source_id]: updated }))
                            }}
                          />
                        </>
                      )}
                    </div>
                  )}
                </article>
              )
            })}
          </div>
        )}
        {metadataSource && (
          <CatalogInspector
            key={metadataSource.source_id}
            source={metadataSource}
            onClose={() => setMetadataSourceId(null)}
            onSourceUpdated={update => {
              if (!update?.source_id || typeof update.profiling_enabled !== 'boolean') return
              setSources(current => current.map(item => item.source_id === update.source_id
                ? { ...item, profiling_enabled: update.profiling_enabled }
                : item))
            }}
          />
        )}
      </section>

      {deleteTarget && (
        <div className="source-delete-confirmation" role="alertdialog" aria-modal="false" aria-labelledby="delete-source-title">
          <div>
            <h3 id="delete-source-title">Delete “{deleteTarget.display_name}”?</h3>
            <p>This removes NeuroNova’s connection configuration only. It does not delete tables or records from the client database.</p>
            {deleteError && <p className="source-alert" role="alert">{deleteError}</p>}
          </div>
          <div className="source-actions">
            <button className="btn btn-ghost-neutral" type="button" onClick={() => setDeleteTarget(null)} disabled={Boolean(deletingId)}>Cancel</button>
            <button className="btn btn-primary source-delete-button" type="button" onClick={deleteSource} disabled={Boolean(deletingId)}>
              {deletingId ? 'Deleting…' : 'Delete connection'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
