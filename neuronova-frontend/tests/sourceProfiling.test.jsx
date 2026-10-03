import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SourceProfilingPanel from '../src/components/sources/SourceProfilingPanel'
import { api } from '../src/api/client'

vi.mock('../src/api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

const sourceId = 'source-profile-1'
const snapshotId = 'snapshot-profile-1'
const asset = {
  asset_id: 'asset-orders',
  schema_name: 'sales',
  table_name: 'orders',
}
const selectedAssetDetail = {
  source_id: sourceId,
  snapshot_id: snapshotId,
  scope_matches_current_selection: true,
  asset,
  columns: [
    {
      column_id: 'column-amount',
      column_name: 'amount',
      native_type_schema: 'pg_catalog',
      native_type_name: 'numeric',
      formatted_native_type: 'numeric(12,2)',
    },
    {
      column_id: 'column-note',
      column_name: 'note',
      native_type_schema: 'pg_catalog',
      native_type_name: 'jsonb',
      formatted_native_type: 'jsonb',
    },
  ],
}

function profileRun(overrides = {}) {
  return {
    run_id: 'profile-run-1',
    state: 'QUEUED',
    snapshot_id: snapshotId,
    requested_at: '2026-10-04T10:00:00Z',
    started_at: null,
    completed_at: null,
    selected_asset_count: 1,
    selected_column_count: 1,
    sample_percent: 1,
    sample_limit: 500,
    max_operations: 10,
    sampling_method: 'SYSTEM_TABLE_SAMPLE',
    ...overrides,
  }
}

function jwtOptions() {
  return expect.objectContaining({ requireJwt: true, preserveErrorBody: true })
}

function renderPanel({ enabled = false, ...overrides } = {}) {
  return render(
    <SourceProfilingPanel
      source={{ source_id: sourceId, display_name: 'Warehouse', profiling_enabled: enabled }}
      sourceId={sourceId}
      snapshotId={snapshotId}
      assets={[asset]}
      selectedAssetDetail={selectedAssetDetail}
      {...overrides}
    />,
  )
}

beforeEach(() => {
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/profile-runs?page=1&page_size=20`) return Promise.resolve({ items: [], count: 0 })
    throw new Error(`unexpected GET ${path}`)
  })
  api.put.mockResolvedValue({
    source_id: sourceId,
    profiling_enabled: true,
    profiling_generation: 1,
    historical_profiles_retained: true,
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/profile-runs`) return Promise.resolve(profileRun())
    throw new Error(`unexpected POST ${path}`)
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('stays default-off until an owner visibly acknowledges and enables profiling', async () => {
  renderPanel()

  expect(screen.getByText(/Profiling is off by default/i)).toBeTruthy()
  expect(api.get).not.toHaveBeenCalled()
  expect(api.post).not.toHaveBeenCalled()

  fireEvent.click(screen.getByRole('button', { name: 'Open profiling' }))
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    `/sources/${sourceId}/profile-runs?page=1&page_size=20`,
    jwtOptions(),
  ))
  expect(api.post).not.toHaveBeenCalled()

  const enable = screen.getByRole('button', { name: 'Enable optional profiling' })
  expect(enable.disabled).toBe(true)
  fireEvent.click(screen.getByRole('checkbox', { name: /I understand that manual profiling reads selected database data/i }))
  expect(enable.disabled).toBe(false)
  fireEvent.click(enable)

  await waitFor(() => expect(api.put).toHaveBeenCalledWith(
    `/sources/${sourceId}/profiling`,
    { profiling_enabled: true },
    jwtOptions(),
  ))
  expect(await screen.findByText(/will not run until you choose a current catalog scope/i)).toBeTruthy()
  expect(api.post).not.toHaveBeenCalled()
})

it('submits only current catalog UUIDs for an explicit bounded profile request', async () => {
  renderPanel({ enabled: true })
  fireEvent.click(screen.getByRole('button', { name: 'Open profiling' }))
  await screen.findByRole('button', { name: 'Request bounded profile' })

  const selectTable = screen.getByRole('checkbox', { name: /"sales"\."orders"/ })
  fireEvent.click(selectTable)
  await screen.findByText(/Supported scalar columns \(1\/1\)/)
  expect(screen.queryByText('"note"')).toBeNull()

  const request = screen.getByRole('button', { name: 'Request bounded profile' })
  expect(request.disabled).toBe(false)
  fireEvent.click(request)

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/profile-runs`,
    {
      tables: [{
        asset_id: 'asset-orders',
        column_ids: ['column-amount'],
      }],
    },
    jwtOptions(),
  ))
  const payload = api.post.mock.calls.find(([path]) => path === `/sources/${sourceId}/profile-runs`)[1]
  expect(JSON.stringify(payload)).not.toContain('sales')
  expect(JSON.stringify(payload)).not.toContain('schema_name')
  expect(JSON.stringify(payload)).not.toContain('table_name')
  expect(JSON.stringify(payload)).not.toContain('sql')
  expect(await screen.findByText(/queued until a worker claims it/i)).toBeTruthy()
})

it('renders aggregate-only sample observations and deterministic findings without raw values', async () => {
  const completed = profileRun({
    run_id: 'profile-run-complete',
    state: 'SUCCEEDED',
    completed_at: '2026-10-04T10:05:00Z',
  })
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/profile-runs?page=1&page_size=20`) return Promise.resolve({ items: [completed], count: 1 })
    if (path === `/sources/${sourceId}/profile-runs/profile-run-complete`) return Promise.resolve(completed)
    if (path === `/sources/${sourceId}/profile-runs/profile-run-complete/observations?page=1&page_size=100`) {
      return Promise.resolve({
        source_id: sourceId,
        run_id: 'profile-run-complete',
        snapshot_id: snapshotId,
        page: 1,
        page_size: 100,
        total_tables: 1,
        table_observations: [{
          observation_id: 'table-observation-1',
          asset_id: 'asset-orders',
          schema_name: 'sales',
          table_name: 'orders',
          observed_row_count: 40,
          status: 'COMPLETE',
          warning_codes: ['system_sample_block_bias'],
          sampling_method: 'SYSTEM_TABLE_SAMPLE',
          sample_percent: 1,
          sample_limit: 500,
          sample_seed: 17,
          population: 'OBSERVED_SAMPLE',
          raw_rows: ['this-must-never-render'],
        }],
        column_observations: [{
          observation_id: 'column-observation-1',
          table_observation_id: 'table-observation-1',
          asset_id: 'asset-orders',
          column_id: 'column-amount',
          column_name: 'amount',
          observed_row_count: 40,
          null_count: 5,
          null_rate: 0.125,
          non_null_count: 35,
          distinct_count: 12,
          numeric_count: 35,
          numeric_sum: '9999999999999999999999999999.1234567890',
          numeric_minimum: 10,
          numeric_maximum: 99,
          numeric_mean: 42.5,
          status: 'COMPLETE',
          warning_codes: [],
          population: 'OBSERVED_SAMPLE',
          raw_value: 'this-must-never-render',
        }],
      })
    }
    if (path === `/sources/${sourceId}/profile-runs/profile-run-complete/findings?page=1&page_size=100`) {
      return Promise.resolve({
        source_id: sourceId,
        run_id: 'profile-run-complete',
        page: 1,
        page_size: 100,
        total: 1,
        items: [{
          finding_id: 'finding-1',
          finding_type: 'OBSERVED_NULL_RATE',
          severity: 'MEDIUM',
          title: 'Observed null rate',
          description: '12.5% of observed rows had a null value.',
          limitations: 'This applies only to the observed sample.',
          column_name: 'amount',
          evidence: { raw_value: 'this-must-never-render' },
        }],
      })
    }
    throw new Error(`unexpected GET ${path}`)
  })
  renderPanel({ enabled: true })
  fireEvent.click(screen.getByRole('button', { name: 'Open profiling' }))
  const inspect = await screen.findByRole('button', { name: 'Inspect observations' })
  fireEvent.click(inspect)

  expect(await screen.findByRole('heading', { name: 'Observed table samples' })).toBeTruthy()
  expect(screen.getAllByText('Observed sample only')).toHaveLength(2)
  expect(screen.getAllByText(/1%/).length).toBeGreaterThan(0)
  expect(screen.queryByText(/100% sample target/i)).toBeNull()
  expect(screen.getAllByText('40')).toHaveLength(2)
  expect(screen.getByText('9999999999999999999999999999.1234567890')).toBeTruthy()
  expect(screen.getByText('12.5% of observed rows had a null value.')).toBeTruthy()
  expect(screen.getByText(/This applies only to the observed sample/i)).toBeTruthy()
  expect(screen.queryByText('this-must-never-render')).toBeNull()
  expect(screen.getByText(/do not establish full-table uniqueness/i)).toBeTruthy()
})

it('uses the source-scoped cancellation endpoint for an active profile', async () => {
  const queued = profileRun()
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/profile-runs?page=1&page_size=20`) return Promise.resolve({ items: [queued], count: 1 })
    throw new Error(`unexpected GET ${path}`)
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/profile-runs/profile-run-1/cancel`) {
      return Promise.resolve({ ...queued, state: 'CANCELLED', completed_at: '2026-10-04T10:01:00Z' })
    }
    throw new Error(`unexpected POST ${path}`)
  })
  renderPanel({ enabled: true })
  fireEvent.click(screen.getByRole('button', { name: 'Open profiling' }))
  const cancel = await screen.findByRole('button', { name: 'Cancel profile' })
  fireEvent.click(cancel)
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/profile-runs/profile-run-1/cancel`,
    {},
    jwtOptions(),
  ))
  expect(await screen.findByText(/No further results from that active run can be published/i)).toBeTruthy()
})
