import { cellString, resultView } from '../format'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

function arrow(text) {
  if (typeof text !== 'string' || text === 'n/a') return ''
  if (text.startsWith('-')) return '▼'
  return /^[+]?0+(?:\.0+)?%?$/.test(text) ? '▬' : '▲'
}

// The delta and the percentage come from the resolved spec: the server computed them from exact decimals. The
// colour stays neutral because "up" is not always good.
export default function KpiComparisonRenderer({ result, spec }) {
  const { columns, rows, index } = resultView(result)
  const [currentLabel, previousLabel] = spec?.encodings?.y ?? []
  const current = cellString(rows[0]?.[index(currentLabel)])
  const previous = cellString(rows[0]?.[index(previousLabel)])
  const delta = spec?.derived?.delta
  const percent = spec?.derived?.delta_pct
  const names = columns.map(([name]) => name)
  return (
    <section aria-label={`${currentLabel} compared with ${previousLabel}`} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 10, padding: '14px 12px', border: '1px solid var(--color-border)', borderRadius: 8, background: 'var(--color-surface, white)' }}>
      <div>
        <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{names.includes(currentLabel) ? currentLabel : 'Current'}</span>
        <strong data-testid="kpi-current" style={{ fontFamily: 'var(--font-heading)', fontSize: 30, color: 'var(--color-text-primary)', overflowWrap: 'anywhere' }}>{current}</strong>
      </div>
      <div>
        <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{names.includes(previousLabel) ? previousLabel : 'Previous'}</span>
        <strong data-testid="kpi-previous" style={{ fontFamily: 'var(--font-heading)', fontSize: 22, color: 'var(--color-text-secondary)', overflowWrap: 'anywhere' }}>{previous}</strong>
      </div>
      <div>
        <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>Change</span>
        <strong data-testid="kpi-delta" style={{ fontFamily: 'var(--font-heading)', fontSize: 22, color: 'var(--color-text-primary)' }}>
          {delta == null ? '—' : <><span aria-hidden="true">{arrow(delta)} </span>{delta}</>}
        </strong>
        <span data-testid="kpi-delta-pct" style={{ display: 'block', fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>{percent ?? '—'}</span>
      </div>
    </section>
  )
}
