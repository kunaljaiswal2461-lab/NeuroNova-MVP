import { checkDisplay, columnsOf } from '../eligibility'

// Every renderer exports ``eligible(result, spec)``: the browser's own C4 check of the same rule the backend ran.
export function eligibleFor(result, spec) {
  if (!result || result.status !== 'succeeded') return { ok: false, reason: 'result_not_successful' }
  return checkDisplay(spec?.display, spec?.encodings, columnsOf(result), Array.isArray(result.rows) ? result.rows : [])
}

export function ChartFrame({ label, note, children }) {
  return (
    <figure aria-label={label} style={{ margin: 0 }}>
      <div role="img" aria-label={label} style={{ width: '100%', height: 280, minWidth: 0, overflowX: 'auto', overflowY: 'hidden' }}>
        <div style={{ minWidth: 320, height: '100%' }}>{children}</div>
      </div>
      {note && <figcaption style={{ marginTop: 3, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{note}</figcaption>}
    </figure>
  )
}
