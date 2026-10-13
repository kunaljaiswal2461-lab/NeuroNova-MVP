// Small presentation helpers shared by the answer renderers. Numbers are never reformatted: a cell is shown as the
// exact string the typed result carries (SPEC-09 W5.3, the Stage 6B numeric policy).
import { columnsOf } from './eligibility'

export const LABEL_LIMIT = 24

export function cellString(cell) {
  if (cell == null || typeof cell !== 'object') return '—'
  if (cell.kind === 'null' || cell.value == null) return '—'
  if (cell.kind === 'boolean') return cell.value ? 'true' : 'false'
  return typeof cell.value === 'string' ? cell.value : '—'
}

export function shortLabel(text, limit = LABEL_LIMIT) {
  const value = String(text ?? '')
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value
}

export function resultView(result) {
  const columns = columnsOf(result)
  const rows = Array.isArray(result?.rows) ? result.rows : []
  const index = label => columns.findIndex(([name]) => name === label)
  return { columns, rows, index }
}

// Long format -> one record per x value with one numeric key per series (``v0``, ``v1`` ...). The keys are positional
// on purpose: a series that happens to be called "x" or "exact" must not overwrite the record's own fields. The data
// is pivoted here, never in SQL and never by guessing: every (x, series) pair is unique by the eligibility check.
export function pivotLong(rows, xIndex, seriesIndex, valueIndex) {
  const xOrder = []
  const names = []
  const byX = new Map()
  for (const row of rows) {
    const x = cellString(row[xIndex])
    const name = cellString(row[seriesIndex])
    if (!byX.has(x)) {
      byX.set(x, { x, exact: {} })
      xOrder.push(x)
    }
    if (!names.includes(name)) names.push(name)
    const cell = row[valueIndex]
    const record = byX.get(x)
    record.exact[name] = cellString(cell)
    record[`v${names.indexOf(name)}`] = cell && cell.value != null ? Number(cell.value) : null
  }
  return { data: xOrder.map(x => byX.get(x)), series: names.map((name, position) => ({ name, key: `v${position}` })) }
}

export const SERIES_COLORS = [
  'var(--color-primary-indigo)', '#0D9488', '#D97706', '#DB2777', '#2563EB', '#65A30D', '#7C3AED', '#0891B2',
  '#EA580C', '#4F46E5', '#BE123C', '#475569',
]

export const chartBox = {
  width: '100%', height: 280, minWidth: 0, overflowX: 'auto', overflowY: 'hidden',
}

export const tickStyle = { fontFamily: 'var(--font-data)', fontSize: 10, fill: 'var(--color-text-muted)' }
