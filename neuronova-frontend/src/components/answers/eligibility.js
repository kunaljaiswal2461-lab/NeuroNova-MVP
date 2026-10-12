// Which display can this result take? (SPEC-09 C4). A second check: the backend already decided, this one only
// makes sure the browser never draws a chart the data cannot support. The rules mirror
// backend/app/services/nl_sql/display.py and both are tested against the same fixture file
// (tests/fixtures/display_eligibility.json). The numeric plotting policy is Stage 6B's, reused from
// databaseChartSpec.js; it applies to the displays that plot numbers, not to KPI, ranked list, pivot or table.
import { canonicalDecimal, plotNumber } from '../charts/databaseChartSpec'

export const OUTPUT_TYPES = [
  'kpi', 'kpi_comparison', 'table', 'ranked_list', 'bar', 'grouped_bar', 'line', 'multi_line',
  'area', 'pie', 'scatter', 'histogram', 'heatmap', 'pivot', 'narrative', 'takeaways',
]

const KIND_BY_PG_TYPE = {
  int2: 'integer', int4: 'integer', int8: 'integer', numeric: 'decimal', float4: 'number', float8: 'number',
  text: 'text', varchar: 'text', bpchar: 'text', bool: 'boolean', uuid: 'uuid',
  date: 'date', timestamp: 'timestamp', timestamptz: 'timestamptz',
}
const NUMERIC = new Set(['integer', 'decimal', 'number'])
const CATEGORY = new Set(['text', 'boolean', 'uuid'])
const TEMPORAL = new Set(['date', 'timestamp', 'timestamptz'])
const CATEGORY_OR_NUMERIC = new Set([...CATEGORY, ...NUMERIC])

export const LIMITS = {
  ranked: 100, bar: 100, groupedX: 100, groupedSeries: 12, multiLineSeries: 8, pie: 6, scatter: 500,
  histogram: 100, heatmapSide: 50, pivotSeries: 30,
}

const OK = { ok: true, reason: null }
const no = reason => ({ ok: false, reason })

const KINDS = new Set(Object.values(KIND_BY_PG_TYPE))

// A postgres type name. A result column that only carries the already-mapped kind ("decimal") is accepted too.
export function kindOf(pgType) {
  return KIND_BY_PG_TYPE[pgType] ?? (KINDS.has(pgType) ? pgType : null)
}

// A typed result column -> [label, pg type]. Both persisted and streamed results carry the actual type.
export function columnsOf(result) {
  return (Array.isArray(result?.columns) ? result.columns : []).map(column => [
    column.alias, column.actual_postgres_type ?? column.expected_postgres_type ?? column.logical_type,
  ])
}

const valueOf = cell => (cell && typeof cell === 'object' ? cell.value : undefined)
const textOf = cell => (typeof valueOf(cell) === 'string' ? cell.value : null)

function categoryText(cell) {
  if (!cell || !CATEGORY.has(cell.kind) || cell.value == null) return null
  if (cell.kind === 'boolean') return cell.value ? 'true' : 'false'
  return textOf(cell)
}

function validYmd(year, month, day) {
  // Years below 1000 are refused so Date.UTC (which maps 0-99 to 1900-1999) agrees with the backend.
  if (year < 1000 || year > 9999 || month < 1 || month > 12 || day < 1) return false
  return day <= new Date(Date.UTC(year, month, 0)).getUTCDate()
}

function validTime(cell) {
  const text = textOf(cell)
  if (text == null) return false
  let body = text
  if (cell.kind === 'date') {
    const m = body.match(/^(\d{4})-(\d{2})-(\d{2})$/)
    return Boolean(m) && validYmd(Number(m[1]), Number(m[2]), Number(m[3]))
  }
  if (cell.kind === 'timestamptz') {
    if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(body)) return false
    body = body.replace(/(?:Z|[+-]\d{2}:\d{2})$/i, '')
  } else if (cell.kind !== 'timestamp') {
    return false
  }
  const m = body.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$/)
  if (!m) return false
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number)
  return validYmd(year, month, day) && hour < 24 && minute < 60 && second < 60
}

const REASON_FROM_PLOT = {
  missing_value: 'missing_value',
  unsupported_value_column: 'value_not_numeric',
  numeric_precision_exceeds_plot_policy: 'numeric_precision_exceeds_plot_policy',
  numeric_magnitude_outside_plot_policy: 'numeric_magnitude_outside_plot_policy',
}

// Stage 6B numeric policy over one column, including the rule that two different exact values must not collapse
// to one plotted number.
function plottable(cells) {
  const seen = new Map()
  for (const cell of cells) {
    const plot = plotNumber(cell)
    if (!plot.eligible) return no(REASON_FROM_PLOT[plot.reason] ?? 'value_not_numeric')
    const known = seen.get(plot.approximation)
    if (known !== undefined && known !== plot.identity) return no('numeric_precision_collision')
    seen.set(plot.approximation, plot.identity)
  }
  return OK
}

function approximation(cell) {
  const plot = plotNumber(cell)
  return plot.eligible ? plot.approximation : 0
}

export function checkDisplay(display, encodings, columns, rows) {
  if (!OUTPUT_TYPES.includes(display)) return no('unknown_display')
  if (display === 'table' || display === 'narrative' || display === 'takeaways') return OK
  const enc = encodings ?? {}
  const labels = new Map()
  columns.forEach(([label], index) => { if (!labels.has(label)) labels.set(label, index) })
  const kinds = columns.map(([, type]) => kindOf(type))
  const col = label => (typeof label === 'string' && labels.has(label) ? labels.get(label) : null)
  const kindAt = position => (position == null ? null : kinds[position])
  const values = position => rows.map(row => row[position])
  const yLabels = (Array.isArray(enc.y) ? enc.y : []).filter(label => typeof label === 'string')
  const count = rows.length

  if (display === 'kpi') {
    if (count !== 1) return no('needs_one_row')
    const position = enc.value ? col(enc.value) : (columns.length === 1 ? 0 : null)
    if (position == null) return no('value_column_missing')
    if (!NUMERIC.has(kindAt(position))) return no('value_not_numeric')
    return valueOf(rows[0][position]) == null ? no('missing_value') : OK
  }
  if (display === 'kpi_comparison') {
    if (count !== 1) return no('needs_one_row')
    if (yLabels.length !== 2) return no('needs_two_numeric_columns')
    const positions = yLabels.map(col)
    if (positions.some(p => p == null)) return no('unknown_label')
    if (positions.some(p => !NUMERIC.has(kindAt(p)))) return no('value_not_numeric')
    if (positions.some(p => valueOf(rows[0][p]) == null)) return no('missing_value')
    return OK
  }
  if (count === 0) return no('empty_result')

  if (display === 'ranked_list') {
    const lp = col(enc.label)
    const vp = col(enc.value)
    if (lp == null || vp == null) return no('unknown_label')
    if (!CATEGORY.has(kindAt(lp))) return no('label_not_category')
    if (!NUMERIC.has(kindAt(vp))) return no('value_not_numeric')
    if (count > LIMITS.ranked) return no('too_many_rows')
    if (rows.some(r => categoryText(r[lp]) == null || valueOf(r[vp]) == null)) return no('missing_value')
    return OK
  }

  if (display === 'bar' || display === 'line' || display === 'area') {
    if (columns.length !== 2) return no('result_shape')
    if (display === 'bar' && !CATEGORY.has(kindAt(0))) return no('x_not_category')
    if (display !== 'bar' && !TEMPORAL.has(kindAt(0))) return no('x_not_temporal')
    if (!NUMERIC.has(kindAt(1))) return no('value_not_numeric')
    if ((enc.x != null && enc.x !== columns[0][0]) || (yLabels.length > 0 && yLabels[0] !== columns[1][0])) {
      return no('encodings_mismatch')
    }
    if (count > LIMITS.bar) return no('too_many_rows')
    const verdict = plottable(values(1))
    if (!verdict.ok) return verdict
    const seen = new Set()
    for (const row of rows) {
      let text
      if (display === 'bar') {
        text = categoryText(row[0])
        if (text == null) return no('missing_value')
        if (seen.has(text)) return no('duplicate_category')
      } else {
        if (!validTime(row[0])) return no('invalid_time')
        text = textOf(row[0]) ?? ''
        if (seen.has(text)) return no('duplicate_time')
      }
      seen.add(text)
    }
    return OK
  }

  if (display === 'pie') {
    const lp = col(enc.label)
    const vp = col(enc.value)
    if (lp == null || vp == null) return no('unknown_label')
    if (!CATEGORY.has(kindAt(lp))) return no('label_not_category')
    if (!NUMERIC.has(kindAt(vp))) return no('value_not_numeric')
    if (count > LIMITS.pie) return no('too_many_rows')
    if (rows.some(r => categoryText(r[lp]) == null)) return no('missing_value')
    const verdict = plottable(values(vp))
    if (!verdict.ok) return verdict
    if (rows.some(r => approximation(r[vp]) < 0)) return no('negative_value')
    return OK
  }

  if (display === 'scatter') {
    const xp = col(enc.x)
    const yp = col(yLabels[0])
    if (xp == null || yp == null) return no('unknown_label')
    if (!NUMERIC.has(kindAt(xp)) || !NUMERIC.has(kindAt(yp))) return no('value_not_numeric')
    if (count > LIMITS.scatter) return no('too_many_rows')
    for (const position of [xp, yp]) {
      const verdict = plottable(values(position))
      if (!verdict.ok) return verdict
    }
    return OK
  }

  if (display === 'histogram') {
    const xp = col(enc.x)
    const yp = col(yLabels[0])
    if (xp == null || yp == null) return no('unknown_label')
    if (!CATEGORY_OR_NUMERIC.has(kindAt(xp))) return no('x_not_category')
    if (!NUMERIC.has(kindAt(yp))) return no('value_not_numeric')
    if (count > LIMITS.histogram) return no('too_many_rows')
    if (rows.some(r => valueOf(r[xp]) == null)) return no('missing_value')
    return plottable(values(yp))
  }

  // grouped_bar, multi_line, heatmap, pivot: x, series and one numeric value.
  const valuePosition = display === 'heatmap' || display === 'pivot' ? col(enc.value) : col(yLabels[0])
  const xp = col(enc.x)
  const sp = col(enc.series)
  if (xp == null || sp == null || valuePosition == null) return no('unknown_label')
  if (!CATEGORY.has(kindAt(sp))) return no('series_not_category')
  if (!NUMERIC.has(kindAt(valuePosition))) return no('value_not_numeric')
  const xKind = kindAt(xp)
  if ((display === 'grouped_bar' || display === 'heatmap') && !CATEGORY.has(xKind)) return no('x_not_category')
  if (display === 'multi_line' && !TEMPORAL.has(xKind)) return no('x_not_temporal')
  if (rows.some(r => categoryText(r[sp]) == null)) return no('missing_value')
  const seriesValues = new Set(rows.map(r => categoryText(r[sp])))
  const xText = cell => (xKind === 'boolean' ? categoryText(cell) : (valueOf(cell) != null ? textOf(cell) : null))
  const xValues = new Set(rows.map(r => xText(r[xp])))
  if (display === 'grouped_bar' || display === 'multi_line' || display === 'heatmap') {
    if (rows.some(r => valueOf(r[xp]) == null)) return no('missing_value')
  }
  if (display === 'multi_line' && rows.some(r => !validTime(r[xp]))) return no('invalid_time')
  const seriesLimit = {
    grouped_bar: LIMITS.groupedSeries, multi_line: LIMITS.multiLineSeries,
    heatmap: LIMITS.heatmapSide, pivot: LIMITS.pivotSeries,
  }[display]
  if (seriesValues.size > seriesLimit) return no('too_many_series')
  if (display === 'grouped_bar' && xValues.size > LIMITS.groupedX) return no('too_many_rows')
  if (display === 'heatmap' && xValues.size > LIMITS.heatmapSide) return no('too_many_rows')
  const pairs = new Set()
  for (const row of rows) {
    const pair = JSON.stringify([xText(row[xp]), categoryText(row[sp])])
    if (pairs.has(pair)) return no('duplicate_pair')
    pairs.add(pair)
  }
  if (display === 'pivot') return OK
  return plottable(values(valuePosition))
}

export { canonicalDecimal }
