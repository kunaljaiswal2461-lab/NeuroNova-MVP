import { useState } from 'react'

// The SQL behind an answer, collapsed by default (SPEC-09 W5.2 item 6). It is rendered as text in a <pre><code>,
// never as HTML, and can be copied.
export default function SqlPanel({ sql }) {
  const [state, setState] = useState('idle')
  if (typeof sql !== 'string' || sql.trim() === '') return null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(sql)
      setState('copied')
    } catch {
      setState('failed')
    }
  }

  return (
    <details style={{ border: '1px solid var(--color-border)', borderRadius: 8, background: 'var(--color-surface, white)' }}>
      <summary style={{ cursor: 'pointer', padding: '8px 12px', fontFamily: 'var(--font-heading)', fontSize: 12, color: 'var(--color-text-primary)' }}>SQL used</summary>
      <div style={{ padding: '0 12px 12px' }}>
        <pre style={{ margin: 0, padding: 10, overflowX: 'auto', maxWidth: '100%', background: 'var(--color-base)', borderRadius: 6, fontFamily: 'var(--font-data)', fontSize: 11, lineHeight: 1.5, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          <code>{sql}</code>
        </pre>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
          <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={copy}>{state === 'copied' ? 'Copied' : 'Copy'}</button>
          {state === 'failed' && <span role="alert" style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: '#9F1239' }}>Copy failed. Select the SQL and copy it by hand.</span>}
        </div>
      </div>
    </details>
  )
}
