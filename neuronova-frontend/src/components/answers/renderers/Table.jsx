import { useState } from 'react'
import { cellString, resultView } from '../format'
import { eligibleFor } from './shared'
import { formatValue } from '../display/formatValue'
import { columnHint, columnProfile, columnTitle } from '../display/columnProfile'
import { FALLBACK_TIME_ZONE } from '../display/locale'

export const eligible = eligibleFor

const INITIAL_ROWS = 50
const NUMERIC = new Set(['integer', 'decimal', 'number'])

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
      ordered.push({ position, name: columns[position][0], title: item.label || columnTitle(item.column, named) })
    }
  }
  columns.forEach(([name], position) => {
    if (!seen.has(position)) ordered.push({ position, name, title: columnTitle(name, named) })
  })
  return ordered
}

function exactOf(cell) {
  if (cell == null || typeof cell !== 'object' || cell.kind === 'null' || cell.value == null) return null
  if (cell.kind === 'boolean') return cell.value ? 'true' : 'false'
  return typeof cell.value === 'string' ? cell.value : cellString(cell)
}

const noRows = (complete) => complete
  ? 'No rows matched this query.'
  : 'No result rows could be safely displayed before a safety limit was reached. This does not mean no rows matched.'

export default function TableRenderer({ result, spec, label = 'Result table' }) {
  const [showAll, setShowAll] = useState(false)
  const { rows } = resultView(result)
  const timeZone = spec?.display_time_zone || FALLBACK_TIME_ZONE
  const columns = orderedColumns(result, spec)
  if (rows.length === 0) {
    return <p style={{ margin: 0, padding: '12px', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }}>{noRows(result?.complete !== false)}</p>
  }
  const shown = showAll ? rows : rows.slice(0, INITIAL_ROWS)
  const profiles = new Map(columns.map(column => [column.position, columnProfile(result, column.position, timeZone)]))
  return (
    <div>
      <div role="region" aria-label={label} tabIndex={0} style={{ overflow: 'auto', maxWidth: '100%', maxHeight: 420, border: '1px solid var(--color-border-light)', borderRadius: 6 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr>
              {columns.map(column => {
                const profile = profiles.get(column.position)
                const numericColumn = NUMERIC.has(profile.kind)
                const hint = columnHint(profile)
                return (
                  <th key={column.position} scope="col" title={column.name} style={{ position: 'sticky', top: 0, zIndex: 1, background: 'var(--color-base)', padding: '7px 10px', fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textAlign: numericColumn ? 'right' : 'left', borderBottom: '1px solid var(--color-border)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                    {column.title}
                    {hint && <small style={{ display: 'block', marginTop: 2, fontFamily: 'var(--font-data)', fontSize: 9, fontWeight: 400, textTransform: 'none' }}>{hint}</small>}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, rowIndex) => (
              <tr key={rowIndex} style={{ borderBottom: '1px solid var(--color-border-light)' }}>
                {columns.map(column => {
                  const profile = profiles.get(column.position)
                  const numericColumn = NUMERIC.has(profile.kind)
                  const formatted = formatValue(exactOf(row[column.position]), profile.kind, { surface: 'table', profile, timeZone })
                  return (
                    <td key={column.position} title={formatted.title} style={{ padding: '7px 10px', fontFamily: 'var(--font-data)', fontSize: 12, color: formatted.empty ? 'var(--color-text-muted)' : 'var(--color-text-primary)', fontStyle: formatted.empty ? 'italic' : 'normal', whiteSpace: 'pre-wrap', textAlign: numericColumn ? 'right' : 'left', fontVariantNumeric: 'tabular-nums' }}>{formatted.text}</td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!showAll && rows.length > INITIAL_ROWS && (
        <p style={{ margin: '6px 0 0', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)' }}>
          Showing {INITIAL_ROWS} of {rows.length} rows · <button type="button" className="btn btn-ghost-neutral btn-sm" onClick={() => setShowAll(true)}>Show all</button>
        </p>
      )}
    </div>
  )
}
