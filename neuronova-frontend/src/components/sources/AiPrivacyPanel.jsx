import { useEffect, useMemo, useState } from 'react'
import { sourceApi, sourceErrorCode } from '../../api/sources'

// What the AI model may see about this source (SPEC-09 W5.5): result rows, example values, and which text columns
// count as personal data. Values are never shown here, only column names and counts.

const MAX_LISTED = 200

const SAFE_ERROR_MESSAGES = {
  authentication_required: 'Your sign-in session is unavailable. Please sign in again.',
  source_not_found: 'This saved connection is no longer available.',
  invalid_column_privacy_request: 'One of those columns is no longer in the current metadata. Refresh the panel and try again.',
  invalid_sample_values_request: 'That example-values change was not valid. Refresh the panel and try again.',
  column_privacy_persistence_failed: 'The change could not be saved. Nothing was changed.',
  sample_values_persistence_failed: 'The change could not be saved. Nothing was changed.',
  rate_limited: 'Too many changes in a short time. Wait a moment and try again.',
}

function safeMessage(error, fallback = 'We could not complete that request. Please try again.') {
  return SAFE_ERROR_MESSAGES[sourceErrorCode(error)] || fallback
}

const keyOf = row => `${row.schema}\u0000${row.table}\u0000${row.column}`
const nameOf = row => `${row.schema}.${row.table}.${row.column}`
const toChoice = state => (state === 'personal' || state === 'not_personal' ? state : 'suggestion')
const fromChoice = choice => (choice === 'personal' || choice === 'not_personal' ? choice : null)

export default function AiPrivacyPanel({ sourceId }) {
  const [opened, setOpened] = useState(false)
  const [phase, setPhase] = useState('loading')
  const [loadError, setLoadError] = useState(null)
  const [loadCode, setLoadCode] = useState(null)
  const [ai, setAi] = useState(null)
  const [samples, setSamples] = useState(null)
  const [privacy, setPrivacy] = useState([])
  const [edits, setEdits] = useState({})
  const [exclusions, setExclusions] = useState(null)
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  const [reloadKey, setReloadKey] = useState(0)

  // Nothing is requested until the owner opens the panel: the inspector stays as light as it was.
  useEffect(() => {
    if (!opened) return undefined
    const controller = new AbortController()
    const { signal } = controller
    const load = async () => {
      try {
        const [settings, sampleState, columns] = await Promise.all([
          sourceApi.getAiSettings(sourceId, { signal }),
          sourceApi.getSampleValues(sourceId, { signal }),
          sourceApi.getColumnPrivacy(sourceId, { signal }),
        ])
        if (signal.aborted) return
        setAi(settings)
        setSamples(sampleState)
        setPrivacy(Array.isArray(columns) ? columns : [])
        setEdits({})
        setExclusions(null)
        setPhase('ready')
      } catch (failure) {
        if (signal.aborted) return
        setLoadCode(sourceErrorCode(failure))
        setLoadError(safeMessage(failure, 'We could not load the AI and privacy settings.'))
        setPhase('error')
      }
    }
    void load()
    return () => controller.abort()
  }, [sourceId, reloadKey, opened])

  const run = async (name, action) => {
    setBusy(name)
    setError(null)
    setNotice(null)
    try {
      await action()
    } catch (failure) {
      setError(safeMessage(failure))
    } finally {
      setBusy(null)
    }
  }

  const toggleRows = event => {
    const wanted = event.target.checked
    void run('rows', async () => {
      const updated = await sourceApi.updateAiSettings(sourceId, { result_rows_to_model: wanted })
      setAi(updated)
      setNotice(wanted ? 'Result rows can now be sent to the AI model.' : 'The AI model no longer sees result rows.')
    })
  }

  const toggleSamples = event => {
    const wanted = event.target.checked
    void run('samples', async () => {
      const updated = await sourceApi.updateSampleValues(sourceId, { enabled: wanted })
      setSamples(updated)
      setAi(current => (current ? { ...current, sample_values_enabled: updated.enabled } : current))
      setNotice(wanted ? 'Example values will be collected at the next metadata refresh.' : 'Stored example values were removed.')
    })
  }

  const excluded = useMemo(() => {
    const base = exclusions ?? new Set((samples?.excluded_columns ?? []).map(keyOf))
    return base
  }, [exclusions, samples])
  const exclusionsChanged = exclusions != null && (
    exclusions.size !== (samples?.excluded_columns ?? []).length
    || (samples?.excluded_columns ?? []).some(item => !exclusions.has(keyOf(item)))
  )

  const toggleExcluded = row => {
    const next = new Set(excluded)
    if (next.has(keyOf(row))) next.delete(keyOf(row))
    else next.add(keyOf(row))
    setExclusions(next)
  }

  const saveExclusions = () => run('exclusions', async () => {
    const wanted = privacy.filter(row => excluded.has(keyOf(row)))
    // A previously excluded column that is no longer listed must not be dropped silently.
    const unlisted = (samples?.excluded_columns ?? []).filter(item => !privacy.some(row => keyOf(row) === keyOf(item)))
    const columns = [...wanted, ...unlisted].map(row => ({ schema: row.schema, table: row.table, column: row.column }))
    const updated = await sourceApi.updateSampleValues(sourceId, { excluded_columns: columns })
    setSamples(updated)
    setExclusions(null)
    setNotice('Excluded columns saved.')
  })

  const changedPrivacy = privacy.filter(row => {
    const edit = edits[keyOf(row)]
    return edit !== undefined && edit !== toChoice(row.state)
  })

  const savePrivacy = () => run('privacy', async () => {
    // Only the rows that changed are sent.
    const changes = changedPrivacy.map(row => ({
      schema: row.schema, table: row.table, column: row.column, state: fromChoice(edits[keyOf(row)]),
    }))
    const updated = await sourceApi.updateColumnPrivacy(sourceId, { changes })
    setPrivacy(Array.isArray(updated) ? updated : privacy)
    setEdits({})
    setNotice('Personal-data settings saved.')
  })

  const needle = search.trim().toLowerCase()
  const visible = privacy.filter(row => !needle || nameOf(row).toLowerCase().includes(needle))
  const shown = visible.slice(0, MAX_LISTED)

  // The inspector already reports a vanished source or an expired sign-in once; repeating it here would double the alert.
  if (phase === 'error' && (loadCode === 'source_not_found' || loadCode === 'authentication_required')) return null

  return (
    <details
      className="source-ai-privacy"
      style={{ marginTop: 16 }}
      onToggle={event => setOpened(event.currentTarget.open)}
    >
      <summary style={{ cursor: 'pointer', fontWeight: 700 }}>AI and privacy</summary>
      <div style={{ display: 'grid', gap: 12, marginTop: 8 }}>
      {phase === 'loading' && <p className="source-empty" role="status">Loading AI and privacy settings…</p>}
      {phase === 'error' && (
        <div>
          <p className="source-alert" role="alert">{loadError}</p>
          <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={() => { setPhase('loading'); setLoadError(null); setLoadCode(null); setReloadKey(key => key + 1) }}>Try again</button>
        </div>
      )}
      {phase === 'ready' && ai && samples && (
        <>
          {error && <p className="source-alert" role="alert">{error}</p>}
          {notice && <p className="source-guidance" role="status">{notice}</p>}

          <div>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontWeight: 600 }}>
              <input type="checkbox" checked={Boolean(ai.result_rows_to_model)} disabled={busy === 'rows'} onChange={toggleRows} />
              Send result rows to the AI model
            </label>
            <p className="source-guidance" style={{ margin: '4px 0 0' }}>
              When off, the model sees only column names and totals. When on, it sees up to 50 result rows; columns marked personal are replaced with placeholders first.
            </p>
          </div>

          <div>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontWeight: 600 }}>
              <input type="checkbox" checked={Boolean(samples.enabled)} disabled={busy === 'samples'} onChange={toggleSamples} />
              Use example values in questions
            </label>
            <p className="source-guidance" style={{ margin: '4px 0 0' }}>
              {samples.collected?.length ?? 0} columns have stored example values; {samples.skipped?.length ?? 0} were skipped. The values themselves are never shown here.
            </p>
          </div>

          {privacy.length > 0 && (
            <div>
              <label htmlFor={`privacy-search-${sourceId}`} style={{ fontWeight: 600 }}>Text columns</label>
              <input
                id={`privacy-search-${sourceId}`}
                type="search"
                value={search}
                onChange={event => setSearch(event.target.value)}
                placeholder="Search columns"
                style={{ display: 'block', margin: '4px 0 8px', maxWidth: 320 }}
              />
              <div role="region" aria-label="Text columns" tabIndex={0} style={{ overflowX: 'auto', maxWidth: '100%' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                  <thead>
                    <tr>
                      <th scope="col" style={{ textAlign: 'left', padding: '4px 8px' }}>Column</th>
                      <th scope="col" style={{ textAlign: 'left', padding: '4px 8px' }}>Suggested</th>
                      <th scope="col" style={{ textAlign: 'left', padding: '4px 8px' }}>Personal data</th>
                      <th scope="col" style={{ textAlign: 'left', padding: '4px 8px' }}>No example values</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map(row => {
                      const choice = edits[keyOf(row)] ?? toChoice(row.state)
                      return (
                        <tr key={keyOf(row)}>
                          <td style={{ padding: '4px 8px', fontFamily: 'var(--font-data)', overflowWrap: 'anywhere' }}>{nameOf(row)}</td>
                          <td style={{ padding: '4px 8px' }}>{row.suggested_personal ? 'Personal' : 'Not personal'}</td>
                          <td style={{ padding: '4px 8px' }}>
                            <select
                              aria-label={`Personal data setting for ${nameOf(row)}`}
                              value={choice}
                              onChange={event => setEdits(current => ({ ...current, [keyOf(row)]: event.target.value }))}
                            >
                              <option value="suggestion">Use suggestion</option>
                              <option value="personal">Personal</option>
                              <option value="not_personal">Not personal</option>
                            </select>
                          </td>
                          <td style={{ padding: '4px 8px' }}>
                            <input
                              type="checkbox"
                              aria-label={`Exclude example values of ${nameOf(row)}`}
                              checked={excluded.has(keyOf(row))}
                              onChange={() => toggleExcluded(row)}
                            />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {visible.length > shown.length && (
                <p className="source-guidance">Showing {shown.length} of {visible.length} matching columns. Search to narrow the list.</p>
              )}
              <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                <button type="button" className="btn btn-primary btn-sm" disabled={changedPrivacy.length === 0 || busy === 'privacy'} onClick={savePrivacy}>
                  Save personal-data changes{changedPrivacy.length > 0 ? ` (${changedPrivacy.length})` : ''}
                </button>
                <button type="button" className="btn btn-ghost-neutral btn-sm" disabled={!exclusionsChanged || busy === 'exclusions'} onClick={saveExclusions}>
                  Save excluded columns
                </button>
              </div>
            </div>
          )}
          {privacy.length === 0 && <p className="source-empty">No text columns in the current metadata.</p>}
        </>
      )}
      </div>
    </details>
  )
}
