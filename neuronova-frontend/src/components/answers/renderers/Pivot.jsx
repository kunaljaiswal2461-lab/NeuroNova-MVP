import { pivotLong, resultView, shortLabel } from '../format'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

// A table pivoted in code: rows = x, columns = series, cells = the exact value.
export default function PivotRenderer({ result, spec }) {
  const { rows, index } = resultView(result)
  const { data, series } = pivotLong(rows, index(spec.encodings.x), index(spec.encodings.series), index(spec.encodings.value))
  return (
    <div role="region" aria-label={`Pivot table of ${spec.encodings.value}`} tabIndex={0} style={{ overflowX: 'auto', maxWidth: '100%' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ background: 'var(--color-base)' }}>
            <th scope="col" style={{ padding: '7px 10px', textAlign: 'left', fontFamily: 'var(--font-heading)', fontSize: 10, color: 'var(--color-text-muted)' }}>{spec.encodings.x}</th>
            {series.map(({ name, key }) => <th key={key} scope="col" title={name} style={{ padding: '7px 10px', textAlign: 'left', fontFamily: 'var(--font-heading)', fontSize: 10, color: 'var(--color-text-muted)' }}>{shortLabel(name)}</th>)}
          </tr>
        </thead>
        <tbody>
          {data.map(record => (
            <tr key={record.x} style={{ borderBottom: '1px solid var(--color-border-light)' }}>
              <th scope="row" title={record.x} style={{ padding: '7px 10px', textAlign: 'left', fontFamily: 'var(--font-data)', fontSize: 12 }}>{shortLabel(record.x)}</th>
              {series.map(({ name, key }) => <td key={key} style={{ padding: '7px 10px', fontFamily: 'var(--font-data)', fontSize: 12 }}>{record.exact[name] ?? ''}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

