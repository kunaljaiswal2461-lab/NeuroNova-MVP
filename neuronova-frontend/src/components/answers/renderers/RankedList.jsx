import { cellString, resultView, shortLabel } from '../format'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

export default function RankedListRenderer({ result, spec }) {
  const { rows, index } = resultView(result)
  const lp = index(spec?.encodings?.label)
  const vp = index(spec?.encodings?.value)
  return (
    <ol aria-label="Ranked results" style={{ margin: 0, paddingLeft: 24, display: 'grid', gap: 5 }}>
      {rows.map((row, position) => {
        const label = cellString(row[lp])
        return (
          <li key={position} style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)' }}>
            <span title={label}>{shortLabel(label)}</span>
            <strong style={{ marginLeft: 8, fontFamily: 'var(--font-data)' }}>{cellString(row[vp])}</strong>
          </li>
        )
      })}
    </ol>
  )
}
