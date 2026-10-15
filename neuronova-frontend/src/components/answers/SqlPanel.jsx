import { useState } from 'react'
import { repairReasonLabel } from './repairLabels'

// The SQL behind an answer, collapsed by default. Rendered as text in <pre><code>, never as HTML, and copyable.
// After a correction it offers both attempts (SPEC-10 W5.5).
function CopyButton({ text }) {
  const [state, setState] = useState('idle')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setState('copied')
    } catch {
      setState('failed')
    }
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
      <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={copy}>{state === 'copied' ? 'Copied' : 'Copy'}</button>
      {state === 'failed' && <span role="alert" style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: '#9F1239' }}>Copy failed. Select the SQL and copy it by hand.</span>}
    </div>
  )
}

function Code({ sql }) {
  return (
    <>
      <pre style={{ margin: 0, padding: 10, overflowX: 'auto', maxWidth: '100%', background: 'var(--color-base)', borderRadius: 6, fontFamily: 'var(--font-data)', fontSize: 11, lineHeight: 1.5, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        <code>{sql}</code>
      </pre>
      <CopyButton text={sql} />
    </>
  )
}

export default function SqlPanel({ sql, firstSql = null, repair = null, title = null }) {
  const [which, setWhich] = useState('final')
  const hasSql = typeof sql === 'string' && sql.trim() !== ''
  const hasFirst = typeof firstSql === 'string' && firstSql.trim() !== '' && firstSql !== sql
  if (!hasSql && !hasFirst) return null
  const corrected = hasSql && hasFirst
  const reason = repairReasonLabel(repair?.reasonCode)
  const summary = title ?? (corrected ? 'SQL used · corrected once' : 'SQL used')
  // A turn that failed after a correction has no final query that ran: show the attempts it made.
  const tabs = [
    ...(hasSql ? [['final', title ? 'Last query' : 'Final query', sql]] : []),
    ...(hasFirst ? [['first', `First attempt (failed: ${reason})`, firstSql]] : []),
  ]
  const active = tabs.find(([key]) => key === which) ?? tabs[0]
  const move = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || tabs.length < 2) return
    event.preventDefault()
    const current = tabs.findIndex(([key]) => key === active[0])
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    setWhich(tabs[next][0])
    event.currentTarget.parentElement?.querySelectorAll('[role="tab"]')[next]?.focus()
  }
  return (
    <details style={{ border: '1px solid var(--color-border)', borderRadius: 8, background: 'var(--color-surface, white)' }}>
      <summary style={{ cursor: 'pointer', padding: '8px 12px', fontFamily: 'var(--font-heading)', fontSize: 12, color: 'var(--color-text-primary)' }}>{summary}</summary>
      <div style={{ padding: '0 12px 12px' }}>
        {tabs.length > 1 && (
          <div role="tablist" aria-label="Query attempts" style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
            {tabs.map(([key, label]) => (
              <button key={key} type="button" role="tab" aria-selected={active[0] === key} tabIndex={active[0] === key ? 0 : -1} onClick={() => setWhich(key)} onKeyDown={move} className="btn btn-ghost-neutral btn-sm">{label}</button>
            ))}
          </div>
        )}
        <Code sql={active[2]} />
      </div>
    </details>
  )
}
