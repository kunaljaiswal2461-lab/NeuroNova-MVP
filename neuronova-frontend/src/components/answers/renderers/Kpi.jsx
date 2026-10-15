import { cellString, resultView } from '../format'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

export default function KpiRenderer({ result, spec }) {
  const { columns, rows, index } = resultView(result)
  const position = spec?.encodings?.value ? index(spec.encodings.value) : 0
  const label = columns[position]?.[0] ?? 'Value'
  const value = cellString(rows[0]?.[position])
  return (
    <section aria-label={`${label} value`} style={{ padding: '14px 12px', border: '1px solid var(--color-border)', borderRadius: 8, background: 'var(--color-surface, white)' }}>
      <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</span>
      <strong data-testid="kpi-value" style={{ display: 'block', marginTop: 4, fontFamily: 'var(--font-heading)', fontSize: 34, lineHeight: 1.15, color: 'var(--color-text-primary)', overflowWrap: 'anywhere' }}>{value}</strong>
    </section>
  )
}
