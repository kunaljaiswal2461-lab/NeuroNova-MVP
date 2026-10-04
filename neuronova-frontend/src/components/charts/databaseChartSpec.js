// Deterministic, presentation-only chart specifications for Stage 6B.
//
// These builders accept only an already-authorized typed query result or
// persisted aggregate profile observations. They never fetch, query, group,
// deduplicate, or otherwise reinterpret database data.

export const DATABASE_CHART_SPEC_VERSION = 'database_chart_v1'

const MAX_PLOT_POINTS = 100
const MAX_SIGNIFICANT_DIGITS = 15
const MAX_PLOT_MAGNITUDE = 1e15
const MIN_NON_ZERO_PLOT_MAGNITUDE = 1e-12
const DECIMAL_PATTERN = /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/
const NUMERIC_KINDS = new Set(['integer', 'decimal', 'number'])
const CATEGORY_KINDS = new Set(['text', 'boolean', 'uuid'])
const TEMPORAL_KINDS = new Set(['date', 'timestamp', 'timestamptz'])

export const CHART_UNAVAILABLE_MESSAGES = {
  result_not_successful: 'Charts are available only for a successful database result.',
  empty_result: 'No chart is available because this result has no returned rows.',
  result_shape_ambiguous: 'No chart is available because this result has more or fewer than one label column and one value column.',
  too_many_points: `No chart is available because this result has more than ${MAX_PLOT_POINTS} displayed rows. The table preserves the full returned result.`,
  unsupported_category_column: 'No chart is available because the first result column is not a supported category or temporal value.',
  unsupported_value_column: 'No chart is available because the value column is not a finite, representable numeric value.',
  missing_value: 'No chart is available because one or more displayed values are missing. Missing values are never plotted as zero.',
  duplicate_category: 'No chart is available because repeated category labels would make the bars ambiguous.',
  duplicate_time: 'No chart is available because repeated timestamps would make the line ambiguous.',
  invalid_time: 'No chart is available because a temporal value is invalid or lacks the required time semantics.',
  numeric_precision_exceeds_plot_policy: 'No chart is available because the numeric precision exceeds the safe plotting policy. Exact values remain in the table.',
  numeric_magnitude_outside_plot_policy: 'No chart is available because the numeric magnitude cannot be plotted meaningfully. Exact values remain in the table.',
  numeric_precision_collision: 'No chart is available because distinct exact values would collapse to the same plotted number. Exact values remain in the table.',
  profile_observations_unavailable: 'No profile chart is available because compatible observed null-rate aggregates were not retained.',
  profile_population_incompatible: 'No profile chart is available because the observations do not describe one compatible observed sample.',
  profile_rate_invalid: 'No profile chart is available because an observed null rate is invalid or unavailable.',
}

function unavailable(reason, extra = {}) {
  return {
    eligible: false,
    reason,
    message: CHART_UNAVAILABLE_MESSAGES[reason] || 'No chart is available for this result.',
    ...extra,
  }
}

function cellText(cell) {
  return cell && typeof cell === 'object' && typeof cell.value === 'string' ? cell.value : null
}

function canonicalDecimal(value) {
  const text = String(value).trim()
  if (!DECIMAL_PATTERN.test(text)) return null
  const match = text.match(/^(-?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/)
  if (!match) return null
  const [, sign, integer = '', fraction = '', exponentText = '0'] = match
  let digits = `${integer}${fraction}`.replace(/^0+/, '')
  if (!digits) return '0'
  let exponent = Number(exponentText) - fraction.length
  const trailingZeroes = (digits.match(/0+$/) || [''])[0].length
  if (trailingZeroes) {
    digits = digits.slice(0, -trailingZeroes)
    exponent += trailingZeroes
  }
  return `${sign === '-' ? '-' : ''}${digits}e${exponent}`
}

function significantDigits(value) {
  const canonical = canonicalDecimal(value)
  if (!canonical || canonical === '0') return 1
  return canonical.replace(/^-/, '').split('e', 1)[0].length
}

function plotNumber(cell) {
  if (!cell || typeof cell !== 'object') return unavailable('unsupported_value_column')
  if (cell.kind === 'null' || cell.value == null) return unavailable('missing_value')
  if (!NUMERIC_KINDS.has(cell.kind)) return unavailable('unsupported_value_column')
  const exact = cellText(cell)
  if (!exact || !DECIMAL_PATTERN.test(exact)) return unavailable('unsupported_value_column')
  if (significantDigits(exact) > MAX_SIGNIFICANT_DIGITS) {
    return unavailable('numeric_precision_exceeds_plot_policy')
  }
  const approximation = Number(exact)
  if (!Number.isFinite(approximation)) return unavailable('unsupported_value_column')
  const magnitude = Math.abs(approximation)
  if (magnitude > MAX_PLOT_MAGNITUDE || (magnitude !== 0 && magnitude < MIN_NON_ZERO_PLOT_MAGNITUDE)) {
    return unavailable('numeric_magnitude_outside_plot_policy')
  }
  const identity = canonicalDecimal(exact)
  if (!identity) return unavailable('unsupported_value_column')
  return { eligible: true, exact, identity, approximation }
}

function dateSortValue(value) {
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (!match) return null
  const [, yearText, monthText, dayText] = match
  const year = Number(yearText)
  const month = Number(monthText)
  const day = Number(dayText)
  const parsed = Date.UTC(year, month - 1, day)
  const check = new Date(parsed)
  return check.getUTCFullYear() === year && check.getUTCMonth() === month - 1 && check.getUTCDate() === day
    ? parsed
    : null
}

function localTimestampSortValue(value) {
  // Timestamp without time zone is sorted by its stated wall-clock fields.
  // We use UTC only as a stable tuple carrier and keep the original text for
  // display; it is never converted to the browser's local zone.
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$/)
  if (!match) return null
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, fractionText = ''] = match
  const year = Number(yearText)
  const month = Number(monthText)
  const day = Number(dayText)
  const hour = Number(hourText)
  const minute = Number(minuteText)
  const second = Number(secondText)
  const milliseconds = Number(`${fractionText}000`.slice(0, 3))
  const parsed = Date.UTC(year, month - 1, day, hour, minute, second, milliseconds)
  const check = new Date(parsed)
  return check.getUTCFullYear() === year
    && check.getUTCMonth() === month - 1
    && check.getUTCDate() === day
    && check.getUTCHours() === hour
    && check.getUTCMinutes() === minute
    && check.getUTCSeconds() === second
    ? parsed
    : null
}

function zonedTimestampSortValue(value) {
  const text = String(value)
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(text)) return null
  const parsed = Date.parse(text)
  return Number.isFinite(parsed) ? parsed : null
}

function temporalValue(cell) {
  if (!cell || typeof cell !== 'object' || !TEMPORAL_KINDS.has(cell.kind)) return null
  const exact = cellText(cell)
  if (!exact) return null
  const sortValue = cell.kind === 'date'
    ? dateSortValue(exact)
    : cell.kind === 'timestamp'
      ? localTimestampSortValue(exact)
      : zonedTimestampSortValue(exact)
  if (sortValue == null) return null
  const semantics = cell.kind === 'date'
    ? 'Date-only values are ordered as stated calendar dates; they are not shifted through the browser time zone.'
    : cell.kind === 'timestamp'
      ? 'Timestamp values without time zone are ordered by their stated wall-clock fields; labels are not shifted through the browser time zone.'
      : 'Timestamp-with-time-zone values are ordered by their stated instants; labels retain the server-returned offset.'
  return { exact, sortValue, semantics }
}

function categoryValue(cell) {
  if (!cell || typeof cell !== 'object' || !CATEGORY_KINDS.has(cell.kind)) return null
  if (cell.value == null) return null
  if (cell.kind === 'boolean') return { exact: cell.value ? 'true' : 'false' }
  const exact = cellText(cell)
  return exact ? { exact } : null
}

function hasApproximationCollision(points) {
  const exactByApproximation = new Map()
  for (const point of points) {
    const key = String(point.valueApproximation)
    const known = exactByApproximation.get(key)
    if (known && known !== point.valueIdentity) return true
    exactByApproximation.set(key, point.valueIdentity)
  }
  return false
}

function chartCoverage(result) {
  const safetyTruncated = result?.complete === false
    || result?.safety_row_truncated === true
    || result?.safety_byte_truncated === true
  return {
    displayed_row_count: Array.isArray(result?.rows) ? result.rows.length : 0,
    returned_row_count: result?.returned_row_count ?? null,
    complete_for_requested_calculation: result?.complete === true,
    explicit_top_n: result?.explicit_top_n === true,
    requested_limit: result?.requested_limit ?? null,
    safety_truncated: safetyTruncated,
    warnings: Array.isArray(result?.warnings) ? result.warnings : [],
  }
}

function resultProvenance(result) {
  return {
    execution_id: result.execution_id ?? null,
    source_id: result.source_id ?? null,
    snapshot_id: result.snapshot_id ?? null,
    asset_id: result.asset_id ?? null,
    metric: result.metric_provenance ?? null,
    relationship: result.relationship_provenance ?? null,
  }
}

function resultChartSpecBase(result, type, xColumn, yColumn, points, time = null) {
  const unit = result?.metric_provenance?.unit_label
  return {
    eligible: true,
    specification_version: DATABASE_CHART_SPEC_VERSION,
    chart_type: type,
    title: `${yColumn.alias || 'Value'} by ${xColumn.alias || 'result'}`,
    provenance: resultProvenance(result),
    coverage: chartCoverage(result),
    columns: {
      x: { index: 0, alias: xColumn.alias ?? 'column_1', logical_type: xColumn.logical_type ?? null },
      y: { index: 1, alias: yColumn.alias ?? 'column_2', logical_type: yColumn.logical_type ?? null },
    },
    axes: {
      x_label: xColumn.alias ?? 'Result',
      y_label: unit ? `${yColumn.alias ?? 'Value'} (${unit})` : (yColumn.alias ?? 'Value'),
      unit: unit ?? null,
    },
    series: [{ key: 'valueApproximation', label: yColumn.alias ?? 'Value' }],
    time,
    plotting_policy: {
      exact_values_available_in_table_and_tooltip: true,
      axis_values_are_approximations: true,
      missing_values_are_not_plotted: true,
      aggregation_or_regrouping_applied: false,
    },
    points,
  }
}

export function buildDatabaseResultChartSpec(result) {
  if (!result || result.status !== 'succeeded') return unavailable('result_not_successful')
  const rows = Array.isArray(result.rows) ? result.rows : []
  const columns = Array.isArray(result.columns) ? result.columns : []
  if (!rows.length) return unavailable('empty_result')
  if (columns.length !== 2 || rows.some(row => !Array.isArray(row) || row.length !== 2)) {
    return unavailable('result_shape_ambiguous')
  }
  if (rows.length > MAX_PLOT_POINTS) return unavailable('too_many_points')

  const xKinds = new Set(rows.map(row => row[0]?.kind))
  if (xKinds.size !== 1) return unavailable('unsupported_category_column')
  const xKind = [...xKinds][0]
  const points = []
  for (const row of rows) {
    const numeric = plotNumber(row[1])
    if (!numeric.eligible) return numeric
    points.push({
      valueExact: numeric.exact,
      valueIdentity: numeric.identity,
      valueApproximation: numeric.approximation,
      sourceRow: row,
    })
  }
  if (hasApproximationCollision(points)) return unavailable('numeric_precision_collision')

  if (TEMPORAL_KINDS.has(xKind)) {
    const seen = new Set()
    for (let index = 0; index < rows.length; index += 1) {
      const temporal = temporalValue(rows[index][0])
      if (!temporal) return unavailable('invalid_time')
      if (seen.has(temporal.sortValue)) return unavailable('duplicate_time')
      seen.add(temporal.sortValue)
      points[index] = {
        ...points[index],
        label: temporal.exact,
        xExact: temporal.exact,
        timeSortValue: temporal.sortValue,
      }
    }
    points.sort((left, right) => left.timeSortValue - right.timeSortValue)
    return resultChartSpecBase(result, 'line', columns[0], columns[1], points, {
      source_kind: xKind,
      session_time_zone: result.session_time_zone ?? null,
      display_order: 'ascending chronology; the table retains the returned ordering',
      semantics: temporalValue(rows[0][0]).semantics,
    })
  }

  if (!CATEGORY_KINDS.has(xKind)) return unavailable('unsupported_category_column')
  const labels = new Set()
  for (let index = 0; index < rows.length; index += 1) {
    const category = categoryValue(rows[index][0])
    if (!category) return unavailable(rows[index][0]?.kind === 'null' ? 'missing_value' : 'unsupported_category_column')
    if (labels.has(category.exact)) return unavailable('duplicate_category')
    labels.add(category.exact)
    points[index] = {
      ...points[index],
      label: category.exact,
      xExact: category.exact,
    }
  }
  return resultChartSpecBase(result, 'bar', columns[0], columns[1], points, {
    source_kind: xKind,
    display_order: 'returned query ordering; no sorting or regrouping applied',
    semantics: 'Category labels are displayed exactly as returned by the authorized query.',
  })
}

function profileObservationGroups(observations) {
  const tables = Array.isArray(observations?.table_observations) ? observations.table_observations : []
  const columns = Array.isArray(observations?.column_observations) ? observations.column_observations : []
  const tablesById = new Map(tables.map(item => [String(item.observation_id ?? item.table_observation_id ?? ''), item]))
  const columnsByTable = new Map()
  for (const column of columns) {
    const key = String(column?.table_observation_id ?? '')
    if (!key) continue
    const group = columnsByTable.get(key) || []
    group.push(column)
    columnsByTable.set(key, group)
  }
  return { tablesById, columnsByTable }
}

export function buildProfileNullRateChartSpecs({ run, observations } = {}) {
  const { tablesById, columnsByTable } = profileObservationGroups(observations)
  const specs = []
  for (const [tableObservationId, columns] of columnsByTable) {
    const table = tablesById.get(tableObservationId)
    if (!table || !columns.length) {
      specs.push(unavailable('profile_observations_unavailable', { table_observation_id: tableObservationId }))
      continue
    }
    if (
      table.population !== 'OBSERVED_SAMPLE'
      || table.status !== 'COMPLETE'
      || !Number.isInteger(Number(table.observed_row_count))
      || Number(table.observed_row_count) <= 0
      || columns.some(column => column.population !== table.population || column.status !== 'COMPLETE')
    ) {
      specs.push(unavailable('profile_population_incompatible', { table_observation_id: tableObservationId }))
      continue
    }
    const labels = new Set()
    const points = []
    let invalid = false
    for (const column of columns) {
      const label = typeof column.column_name === 'string' && column.column_name ? column.column_name : null
      const numeric = plotNumber({ kind: 'decimal', value: String(column.null_rate ?? '') })
      if (!label || !numeric.eligible || numeric.approximation < 0 || numeric.approximation > 1 || labels.has(label)) {
        invalid = true
        break
      }
      labels.add(label)
      points.push({
        label,
        xExact: label,
        valueExact: numeric.exact,
        valueIdentity: numeric.identity,
        valueApproximation: numeric.approximation * 100,
        nullCount: column.null_count,
        observedRowCount: column.observed_row_count,
        columnObservationId: column.observation_id ?? null,
      })
    }
    if (invalid || hasApproximationCollision(points)) {
      specs.push(unavailable('profile_rate_invalid', { table_observation_id: tableObservationId }))
      continue
    }
    specs.push({
      eligible: true,
      specification_version: DATABASE_CHART_SPEC_VERSION,
      chart_type: 'bar',
      title: `Observed null rates for ${table.schema_name && table.table_name ? `"${table.schema_name}"."${table.table_name}"` : 'one profiled table'}`,
      provenance: {
        source_id: observations?.source_id ?? null,
        run_id: observations?.run_id ?? run?.run_id ?? null,
        snapshot_id: observations?.snapshot_id ?? run?.snapshot_id ?? null,
        table_observation_id: table.observation_id ?? tableObservationId,
        column_observation_ids: points.map(point => point.columnObservationId),
      },
      coverage: {
        population: table.population,
        observed_row_count: table.observed_row_count,
        sampling_method: table.sampling_method,
        sample_percent: table.sample_percent,
        sample_limit: table.sample_limit,
        sample_seed: table.sample_seed,
        collected_at: table.collected_at,
        warnings: table.warning_codes ?? [],
      },
      columns: {
        x: { alias: 'Column', observation_field: 'column_name' },
        y: { alias: 'Observed null rate', observation_field: 'null_rate' },
      },
      axes: { x_label: 'Column', y_label: 'Observed null rate (%)', unit: 'percent' },
      series: [{ key: 'valueApproximation', label: 'Observed null rate (%)' }],
      time: {
        display_order: 'catalog column order returned by the profile observation endpoint',
        semantics: 'Collection time identifies when this bounded sample was observed; it is not business-data freshness.',
      },
      plotting_policy: {
        exact_values_available_in_table_and_tooltip: true,
        axis_values_are_percent_approximations: true,
        missing_values_are_not_plotted: true,
        aggregation_or_regrouping_applied: false,
      },
      profile_sampling_notice: 'Observed sample only. PostgreSQL SYSTEM sampling is block based and, with a row limit, is not presented as a uniformly random or representative whole-table population.',
      points,
    })
  }
  return specs
}
