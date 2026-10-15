import { resultView } from '../format'
import { formatValue } from '../display/formatValue'
import { columnProfile } from '../display/columnProfile'
import { FALLBACK_TIME_ZONE } from '../display/locale'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

export default function KpiRenderer({ result, spec }) {
  const { columns, rows, index } = resultView(result)
  const position = spec?.encodings?.value ? index(spec.encodings.value) : 0
  const label = columns[position]?.[0] ?? 'Value'
  const cell = rows[0]?.[position]
  const exact = cell == null || cell.kind === 'null' || cell.value == null ? null : (cell.kind === 'boolean' ? String(Boolean(cell.value)) : String(cell.value))
  const timeZone = spec?.display_time_zone || FALLBACK_TIME_ZONE
  const profile = columnProfile(result, position, timeZone)
  const shown = formatValue(exact, profile.kind, { surface: 'kpi', profile, timeZone })
  return (
    <section aria-label={`${label} value`} style={{ padding: '14px 12px', border: '1px solid var(--color-border)', borderRadius: 8, background: 'var(--color-surface, white)' }}>
      <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</span>
      <strong data-testid="kpi-value" title={shown.title} tabIndex={0} style={{ display: 'block', marginTop: 4, fontFamily: 'var(--font-heading)', fontSize: 34, lineHeight: 1.15, color: 'var(--color-text-primary)', overflowWrap: 'anywhere' }}>{shown.text}</strong>
    </section>
  )
}
