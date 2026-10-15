import { cellString, resultView } from '../format'
import { eligibleFor } from './shared'

export const eligible = eligibleFor

// Columns follow ``spec.table_columns`` (order and display names); every remaining column comes after them.
export function orderedColumns(result, spec) {
  const { columns } = resultView(result)
  const named = Array.isArray(spec?.table_columns) ? spec.table_columns : []
  const seen = new Set()
  const ordered = []
  for (const item of named) {
    const position = columns.findIndex(([name]) => name === item.column)
    if (position >= 0 && !seen.has(position)) {
      seen.add(position)
      ordered.push({ position, name: columns[position][0], title: item.label || item.column })
    }
  }
  columns.forEach(([name], position) => {
    if (!seen.has(position)) ordered.push({ position, name, title: name })
  })
  return ordered
}

export default function TableRenderer({ result, spec, label = 'Result table' }) {
  const { rows } = resultView(result)
  const columns = orderedColumns(result, spec)
  if (rows.length === 0) {
    return <p style={{ margin: 0, padding: '12px', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }}>No rows matched this query.</p>
  }
  return (
    <div role="region" aria-label={label} tabIndex={0} style={{ overflowX: 'auto', maxWidth: '100%' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ background: 'var(--color-base)' }}>
            {columns.map(column => (
              <th key={column.position} scope="col" title={column.name} style={{ padding: '7px 10px', fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textAlign: 'left', borderBottom: '1px solid var(--color-border)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{column.title}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex} style={{ borderBottom: '1px solid var(--color-border-light)' }}>
              {columns.map(column => (
                <td key={column.position} title={row[column.position]?.kind} style={{ padding: '7px 10px', fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)', whiteSpace: 'pre-wrap' }}>{cellString(row[column.position])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
