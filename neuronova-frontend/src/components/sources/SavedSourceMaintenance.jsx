import { useMemo, useState } from 'react'
import { sourceApi, sourceErrorCode } from '../../api/sources'

const ERROR_MESSAGES = {
  connection_failed: 'We could not connect with the saved connection. Check the database access and try again.',
  connection_timeout: 'The database did not respond in time. Try again after checking its network access.',
  tls_verification_failed: 'The database TLS certificate could not be verified.',
  invalid_source_scope: 'One or more selected tables are no longer eligible. Refresh the table list and choose again.',
}

function errorText(error) {
  return ERROR_MESSAGES[sourceErrorCode(error)] || 'We could not complete that connection maintenance request.'
}

function keyOf(table) {
  return JSON.stringify([table.schema_name, table.table_name])
}

function tableLabel(table) {
  return `"${table.schema_name.replace(/"/g, '""')}"."${table.table_name.replace(/"/g, '""')}"`
}

/**
 * A narrow saved-source maintenance surface.  It deliberately accepts no
 * host, port, database name, SQL, or schedule expression; the backend keeps
 * the endpoint immutable and owns interval validation.
 */
export default function SavedSourceMaintenance({ source, detail, onUpdated }) {
  const [busy, setBusy] = useState(null)
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)
  const [inventory, setInventory] = useState(null)
  const [selected, setSelected] = useState(() => detail?.selected_tables || [])
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [customCaPem, setCustomCaPem] = useState('')
  const [interval, setInterval] = useState(
    source.catalog_refresh_enabled ? String(source.catalog_refresh_interval_minutes || 60) : 'off',
  )

  const selectedKeys = useMemo(() => new Set(selected.map(keyOf)), [selected])

  function received(nextSource) {
    if (nextSource) onUpdated(nextSource)
  }

  async function retest() {
    setBusy('retest'); setError(null); setMessage(null)
    try {
      const result = await sourceApi.retestSavedSource(source.source_id)
      setInventory(result.eligible_tables || [])
      setSelected(result.source?.selected_tables || detail?.selected_tables || [])
      received(result.source)
      setMessage('Connection retested. You can now review the current eligible table list.')
    } catch (requestError) {
      setError(errorText(requestError))
    } finally { setBusy(null) }
  }

  async function replaceCredentials(event) {
    event.preventDefault()
    if (!username || !password) { setError('Enter both a username and password to replace credentials.'); return }
    setBusy('credentials'); setError(null); setMessage(null)
    try {
      const updated = await sourceApi.replaceSavedSourceCredentials(source.source_id, {
        credentials: { username, password },
        tls: { mode: 'verify-full', ...(customCaPem.trim() ? { custom_ca_pem: customCaPem.trim() } : {}) },
      })
      setUsername(''); setPassword(''); setCustomCaPem('')
      received(updated)
      setMessage('Credentials were validated and replaced. Any in-flight connector work was fenced.')
    } catch (requestError) { setError(errorText(requestError)) } finally { setBusy(null) }
  }

  function toggle(table, checked) {
    setSelected(current => checked
      ? [...current.filter(item => keyOf(item) !== keyOf(table)), table]
      : current.filter(item => keyOf(item) !== keyOf(table)))
  }

  async function saveScope() {
    if (!selected.length) { setError('Select at least one eligible table.'); return }
    setBusy('scope'); setError(null); setMessage(null)
    try {
      const updated = await sourceApi.updateSavedSourceScope(source.source_id, { selected_tables: selected })
      received(updated)
      setMessage('Selected table scope updated. Refresh metadata before querying newly included tables.')
    } catch (requestError) { setError(errorText(requestError)) } finally { setBusy(null) }
  }

  async function saveSchedule(nextInterval) {
    setInterval(nextInterval); setBusy('schedule'); setError(null); setMessage(null)
    try {
      const body = nextInterval === 'off'
        ? { enabled: false }
        : { enabled: true, interval_minutes: Number(nextInterval) }
      const policy = await sourceApi.configureCatalogRefresh(source.source_id, body)
      received({ ...source, catalog_refresh_enabled: policy.enabled, catalog_refresh_interval_minutes: policy.interval_minutes, catalog_refresh_next_due_at: policy.next_due_at })
      setMessage(policy.enabled ? 'Scheduled structural refresh enabled.' : 'Scheduled structural refresh disabled.')
    } catch (requestError) { setError(errorText(requestError)) } finally { setBusy(null) }
  }

  return (
    <section className="source-detail" aria-label={`Maintain ${source.display_name}`}>
      <h5>Maintain connection</h5>
      <p className="source-selection-help">The host and database name are fixed after onboarding. Connection maintenance only retests this endpoint, changes protected credentials, adjusts the explicit table scope, or schedules structural metadata refreshes.</p>
      {error && <p className="source-alert" role="alert">{error}</p>}
      {message && <p className="source-notice" role="status">{message}</p>}
      <div className="source-actions">
        <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={retest} disabled={Boolean(busy)}>
          {busy === 'retest' ? 'Retesting…' : 'Retest saved connection'}
        </button>
        <label className="source-field">
          <span>Metadata refresh</span>
          <select value={interval} onChange={event => saveSchedule(event.target.value)} disabled={Boolean(busy)}>
            <option value="off">Off (manual only)</option>
            <option value="15">Every 15 minutes</option>
            <option value="60">Every hour</option>
            <option value="1440">Every day</option>
          </select>
        </label>
      </div>
      <p className="source-previous-test">Last metadata success: {source.catalog_last_success_at ? new Date(source.catalog_last_success_at).toLocaleString() : 'none recorded'}{source.catalog_last_failure_code ? ` · Latest failure: ${source.catalog_last_failure_code}` : ''}</p>

      <form className="source-form-grid" onSubmit={replaceCredentials}>
        <label className="source-field"><span>Replacement username</span><input autoComplete="off" value={username} onChange={event => setUsername(event.target.value)} disabled={Boolean(busy)} /></label>
        <label className="source-field"><span>Replacement password</span><input type="password" autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)} disabled={Boolean(busy)} /></label>
        <label className="source-field source-field-wide"><span>Replacement custom CA PEM (optional)</span><textarea rows={3} value={customCaPem} onChange={event => setCustomCaPem(event.target.value)} disabled={Boolean(busy)} /></label>
        <div className="source-actions"><button className="btn btn-ghost-neutral btn-sm" type="submit" disabled={Boolean(busy)}>{busy === 'credentials' ? 'Validating…' : 'Replace credentials'}</button></div>
      </form>

      {inventory && (
        <div className="source-schema-list">
          <h5>Current eligible scope</h5>
          <p className="source-selection-help">A scope update rechecks the saved credentials and may make prior metadata, profiles, and result rows unavailable until a fresh metadata sync completes.</p>
          {inventory.map(table => (
            <label className="source-table-toggle" key={keyOf(table)}>
              <input type="checkbox" checked={selectedKeys.has(keyOf(table))} onChange={event => toggle(table, event.target.checked)} disabled={Boolean(busy)} />
              <span>{tableLabel(table)}</span>
            </label>
          ))}
          <div className="source-actions"><button className="btn btn-primary btn-sm" type="button" onClick={saveScope} disabled={Boolean(busy) || !selected.length}>{busy === 'scope' ? 'Updating scope…' : `Save scope (${selected.length})`}</button></div>
        </div>
      )}
    </section>
  )
}
