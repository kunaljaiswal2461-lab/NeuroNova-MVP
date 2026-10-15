import { resultView } from '../format'
import { formatValue } from '../display/formatValue'
import { columnProfile } from '../display/columnProfile'
import { FALLBACK_TIME_ZONE } from '../display/locale'
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
  const timeZone = spec?.display_time_zone || FALLBACK_TIME_ZONE
  const show = (label, exactOverride, kindOverride) => {
    const position = index(label)
    const cell = rows[0]?.[position]
    const exact = exactOverride ?? (cell == null || cell.kind === 'null' || cell.value == null ? null : String(cell.value))
    const profile = columnProfile(result, position, timeZone)
    return formatValue(exact, kindOverride ?? profile.kind, { surface: 'kpi', profile, timeZone })
  }
  const current = show(currentLabel)
  const previous = show(previousLabel)
  const deltaExact = spec?.derived?.delta
  const percentExact = spec?.derived?.delta_pct
  const delta = deltaExact == null ? null : formatValue(deltaExact, 'decimal', { surface: 'kpi' })
  const percent = percentExact == null ? null : formatValue(percentExact, percentExact === 'n/a' ? 'text' : 'percent', { surface: 'kpi' })
  const names = columns.map(([name]) => name)
  return (
    <section aria-label={`${currentLabel} compared with ${previousLabel}`} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 10, padding: '14px 12px', border: '1px solid var(--color-border)', borderRadius: 8, background: 'var(--color-surface, white)' }}>
      <div>
        <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{names.includes(currentLabel) ? currentLabel : 'Current'}</span>
        <strong data-testid="kpi-current" title={current.title} tabIndex={0} style={{ fontFamily: 'var(--font-heading)', fontSize: 30, color: 'var(--color-text-primary)', overflowWrap: 'anywhere' }}>{current.text}</strong>
      </div>
      <div>
        <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{names.includes(previousLabel) ? previousLabel : 'Previous'}</span>
        <strong data-testid="kpi-previous" title={previous.title} tabIndex={0} style={{ fontFamily: 'var(--font-heading)', fontSize: 22, color: 'var(--color-text-secondary)', overflowWrap: 'anywhere' }}>{previous.text}</strong>
      </div>
      <div>
        <span style={{ display: 'block', fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>Change</span>
        <strong data-testid="kpi-delta" title={delta ? delta.title : undefined} tabIndex={delta ? 0 : undefined} style={{ fontFamily: 'var(--font-heading)', fontSize: 22, color: 'var(--color-text-primary)' }}>
          {delta == null ? '—' : <><span aria-hidden="true">{arrow(deltaExact)} </span>{delta.text}</>}
        </strong>
        <span data-testid="kpi-delta-pct" title={percent ? percent.title : undefined} tabIndex={percent ? 0 : undefined} style={{ display: 'block', fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>{percent ? percent.text : '—'}</span>
      </div>
    </section>
  )
}
