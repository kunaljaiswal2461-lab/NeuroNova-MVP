import { describe, expect, it } from 'vitest'
import { buildDatabaseResultChartSpec, buildProfileNullRateChartSpecs } from '../src/components/charts/databaseChartSpec'

function result(overrides = {}) {
  return {
    execution_id: 'execution-1',
    status: 'succeeded',
    source_id: 'source-1',
    snapshot_id: 'snapshot-1',
    columns: [
      { alias: 'region', logical_type: 'text' },
      { alias: 'revenue', logical_type: 'decimal' },
    ],
    rows: [
      [{ kind: 'text', value: 'West' }, { kind: 'decimal', value: '-5.00' }],
      [{ kind: 'text', value: 'East' }, { kind: 'decimal', value: '10.25' }],
    ],
    returned_row_count: 2,
    complete: true,
    explicit_top_n: true,
    requested_limit: 2,
    ...overrides,
  }
}

describe('database result chart specifications', () => {
  it('builds a bounded bar chart without regrouping and retains exact numeric text', () => {
    const spec = buildDatabaseResultChartSpec(result())

    expect(spec.eligible).toBe(true)
    expect(spec.chart_type).toBe('bar')
    expect(spec.points.map(point => point.label)).toEqual(['West', 'East'])
    expect(spec.points.map(point => point.valueExact)).toEqual(['-5.00', '10.25'])
    expect(spec.coverage.explicit_top_n).toBe(true)
    expect(spec.coverage.safety_truncated).toBe(false)
    expect(spec.plotting_policy.aggregation_or_regrouping_applied).toBe(false)
  })

  it('orders date labels chronologically without changing their stated calendar dates', () => {
    const spec = buildDatabaseResultChartSpec(result({
      columns: [{ alias: 'day', logical_type: 'date' }, { alias: 'orders', logical_type: 'integer' }],
      rows: [
        [{ kind: 'date', value: '2026-10-02' }, { kind: 'integer', value: '2' }],
        [{ kind: 'date', value: '2026-10-01' }, { kind: 'integer', value: '1' }],
      ],
    }))

    expect(spec.eligible).toBe(true)
    expect(spec.chart_type).toBe('line')
    expect(spec.points.map(point => point.label)).toEqual(['2026-10-01', '2026-10-02'])
    expect(spec.time.semantics).toMatch(/not shifted through the browser time zone/i)
  })

  it('treats a hostile-looking category as ordinary result text', () => {
    const label = '<img src=x onerror=alert(1)>'
    const spec = buildDatabaseResultChartSpec(result({ rows: [[{ kind: 'text', value: label }, { kind: 'decimal', value: '1' }]] }))
    expect(spec).toMatchObject({ eligible: true })
    expect(spec.points[0].label).toBe(label)
  })

  it.each([
    ['duplicate_category', result({ rows: [
      [{ kind: 'text', value: 'East' }, { kind: 'decimal', value: '1' }],
      [{ kind: 'text', value: 'East' }, { kind: 'decimal', value: '2' }],
    ] })],
    ['duplicate_time', result({
      columns: [{ alias: 'when', logical_type: 'timestamptz' }, { alias: 'orders', logical_type: 'integer' }],
      rows: [
        [{ kind: 'timestamptz', value: '2026-10-04T00:00:00Z' }, { kind: 'integer', value: '1' }],
        [{ kind: 'timestamptz', value: '2026-10-04T00:00:00+00:00' }, { kind: 'integer', value: '2' }],
      ],
    })],
    ['missing_value', result({ rows: [[{ kind: 'text', value: 'East' }, { kind: 'null', value: null }]] })],
    ['unsupported_value_column', result({ rows: [[{ kind: 'text', value: 'East' }, { kind: 'number', value: 'Infinity' }]] })],
    ['numeric_precision_exceeds_plot_policy', result({ rows: [[{ kind: 'text', value: 'East' }, { kind: 'integer', value: '9007199254740993' }]] })],
  ])('falls back to the table for %s', (reason, unsafeResult) => {
    const spec = buildDatabaseResultChartSpec(unsafeResult)
    expect(spec).toMatchObject({ eligible: false, reason })
  })
})

describe('profile null-rate chart specifications', () => {
  it('keeps each observed table sample separate and exposes its collection scope', () => {
    const specs = buildProfileNullRateChartSpecs({
      run: { run_id: 'run-1', snapshot_id: 'snapshot-1' },
      observations: {
        source_id: 'source-1',
        table_observations: [{
          observation_id: 'table-1', schema_name: 'sales', table_name: 'orders', status: 'COMPLETE', population: 'OBSERVED_SAMPLE',
          observed_row_count: 40, sampling_method: 'SYSTEM_TABLE_SAMPLE', sample_percent: 1, sample_limit: 500, sample_seed: 17,
          collected_at: '2026-10-04T10:00:00Z', warning_codes: ['system_sample_block_bias'],
        }, {
          observation_id: 'table-2', schema_name: 'sales', table_name: 'customers', status: 'COMPLETE', population: 'OBSERVED_SAMPLE',
          observed_row_count: 20, sampling_method: 'SYSTEM_TABLE_SAMPLE', sample_percent: 1, sample_limit: 500,
        }],
        column_observations: [{
          observation_id: 'column-1', table_observation_id: 'table-1', column_name: 'amount', status: 'COMPLETE', population: 'OBSERVED_SAMPLE',
          observed_row_count: 40, null_count: 5, null_rate: '0.125',
        }, {
          observation_id: 'column-2', table_observation_id: 'table-2', column_name: 'email', status: 'COMPLETE', population: 'OBSERVED_SAMPLE',
          observed_row_count: 20, null_count: 0, null_rate: '0',
        }],
      },
    })

    expect(specs).toHaveLength(2)
    expect(specs.every(spec => spec.eligible)).toBe(true)
    expect(specs.map(spec => spec.provenance.table_observation_id)).toEqual(['table-1', 'table-2'])
    expect(specs[0].coverage).toMatchObject({ population: 'OBSERVED_SAMPLE', observed_row_count: 40, sample_percent: 1 })
    expect(specs[0].profile_sampling_notice).toMatch(/Observed sample only/)
    expect(specs[0].points[0]).toMatchObject({ label: 'amount', valueExact: '0.125', valueApproximation: 12.5 })
  })

  it('does not chart incompatible profile populations', () => {
    const [spec] = buildProfileNullRateChartSpecs({
      observations: {
        table_observations: [{ observation_id: 'table-1', population: 'WHOLE_TABLE', status: 'COMPLETE', observed_row_count: 10 }],
        column_observations: [{ table_observation_id: 'table-1', population: 'WHOLE_TABLE', status: 'COMPLETE', column_name: 'amount', null_rate: '0.1' }],
      },
    })
    expect(spec).toMatchObject({ eligible: false, reason: 'profile_population_incompatible' })
  })
})
