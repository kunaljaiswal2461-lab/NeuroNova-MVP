import { plotNumber } from '../../charts/databaseChartSpec'
import { cellString, resultView, shortLabel } from '../format'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

// A CSS grid: two categories and a number. Colour intensity is linear between the minimum and maximum; the exact
// value is the cell's title and its text.
export default function HeatmapRenderer({ result, spec }) {
  const { rows, index } = resultView(result)
  const xp = index(spec.encodings.x)
  const sp = index(spec.encodings.series)
  const vp = index(spec.encodings.value)
  const xs = []
  const ss = []
  const cells = new Map()
  for (const row of rows) {
    const x = cellString(row[xp])
    const s = cellString(row[sp])
    if (!xs.includes(x)) xs.push(x)
    if (!ss.includes(s)) ss.push(s)
    cells.set(`${x}\u0000${s}`, { exact: cellString(row[vp]), approximation: plotNumber(row[vp]).approximation })
  }
  const approximations = [...cells.values()].map(cell => cell.approximation)
  const low = Math.min(...approximations)
  const high = Math.max(...approximations)
  const intensity = value => (high === low ? 0.5 : (value - low) / (high - low))
  return (
    <div role="region" aria-label={`Heatmap of ${spec.encodings.value} by ${spec.encodings.x} and ${spec.encodings.series}`} tabIndex={0} style={{ overflowX: 'auto', maxWidth: '100%' }}>
      <div style={{ display: 'grid', gridTemplateColumns: `minmax(90px, auto) repeat(${ss.length}, minmax(56px, 1fr))`, gap: 2, minWidth: 320 }}>
        <span />
        {ss.map(s => <strong key={s} title={s} style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{shortLabel(s)}</strong>)}
        {xs.map(x => (
          <div key={x} style={{ display: 'contents' }}>
            <strong title={x} style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', alignSelf: 'center' }}>{shortLabel(x)}</strong>
            {ss.map(s => {
              const cell = cells.get(`${x}\u0000${s}`)
              return (
                <span key={s} title={cell ? cell.exact : 'no value'} style={{ padding: '6px 4px', textAlign: 'center', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)', background: cell ? `color-mix(in srgb, var(--color-primary-indigo) ${Math.round(intensity(cell.approximation) * 70 + 8)}%, transparent)` : 'transparent', border: '1px solid var(--color-border-light)', overflowWrap: 'anywhere' }}>{cell ? cell.exact : ''}</span>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
