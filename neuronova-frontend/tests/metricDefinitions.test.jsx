import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import MetricDefinitionsPanel from '../src/components/sources/MetricDefinitionsPanel'
import { api } from '../src/api/client'

vi.mock('../src/api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const sourceId = 'source-1'
const snapshotId = 'snapshot-1'
const selectedAssetDetail = {
  source_id: sourceId,
  snapshot_id: snapshotId,
  scope_matches_current_selection: true,
  asset: {
    asset_id: 'asset-orders',
    schema_name: 'sales',
    table_name: 'orders',
  },
  columns: [
    {
      column_id: 'column-order-id',
      column_name: 'order_id',
      formatted_native_type: 'uuid',
      native_type_name: 'uuid',
    },
    {
      column_id: 'column-amount',
      column_name: 'amount',
      formatted_native_type: 'numeric(12,2)',
      native_type_name: 'numeric',
    },
    {
      column_id: 'column-created-at',
      column_name: 'created_at',
      formatted_native_type: 'timestamp with time zone',
      native_type_name: 'timestamptz',
    },
  ],
}

function metric(versionNumber = 1) {
  return {
    metric_id: 'metric-revenue',
    version_number: versionNumber,
    name: 'Confirmed revenue',
    description: 'Revenue from the selected authorized table.',
    snapshot_id: snapshotId,
    asset_id: 'asset-orders',
    aggregation: { operation: 'sum', column_id: 'column-amount', column_name: 'amount' },
    fixed_filters: [],
    unit_label: 'USD',
    default_date_column_id: 'column-created-at',
    default_date_column_name: 'created_at',
    default_time_zone: 'UTC',
    confirmed_by_user_id: 'user-1',
    confirmed_at: '2026-10-03T12:00:00Z',
    state: 'valid',
  }
}

function renderPanel() {
  return render(
    <MetricDefinitionsPanel
      sourceId={sourceId}
      snapshotId={snapshotId}
      selectedAssetDetail={selectedAssetDetail}
    />,
  )
}

function jwtOptions() {
  return expect.objectContaining({ requireJwt: true, preserveErrorBody: true })
}

beforeEach(() => {
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/metrics`) return Promise.resolve({ items: [], count: 0 })
    throw new Error(`unexpected GET ${path}`)
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/metrics`) return Promise.resolve(metric())
    throw new Error(`unexpected POST ${path}`)
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('requires visible owner confirmation and posts a typed source-scoped metric definition', async () => {
  renderPanel()

  fireEvent.click(screen.getByRole('button', { name: 'Manage metric definitions' }))
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(`/sources/${sourceId}/metrics`, jwtOptions()))
  fireEvent.click(screen.getByRole('button', { name: 'New metric definition' }))

  fireEvent.change(screen.getByRole('textbox', { name: 'Metric name' }), { target: { value: 'Confirmed revenue' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Aggregate' }), { target: { value: 'sum' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Aggregate column' }), { target: { value: 'column-amount' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Unit label (optional)' }), { target: { value: 'USD' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Default date column (optional)' }), { target: { value: 'column-created-at' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Default time zone (optional)' }), { target: { value: 'UTC' } })

  const save = screen.getByRole('button', { name: 'Save confirmed metric' })
  expect(save.disabled).toBe(true)
  fireEvent.click(screen.getByRole('checkbox', { name: /I confirm this is the intended reusable business definition/i }))
  fireEvent.click(save)

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/metrics`,
    {
      name: 'Confirmed revenue',
      owner_confirmed: true,
      snapshot_id: snapshotId,
      asset_id: 'asset-orders',
      aggregation: { operation: 'sum', column_id: 'column-amount' },
      unit_label: 'USD',
      default_date_column_id: 'column-created-at',
      default_time_zone: 'UTC',
    },
    jwtOptions(),
  ))
  expect(await screen.findByText('Metric definition confirmed. It is available only when the current catalog remains compatible.')).toBeTruthy()
})

it('creates an immutable revision through the source-scoped versions endpoint', async () => {
  const firstVersion = {
    ...metric(),
    // Immutable version references remain historical after a compatible
    // catalog refresh. The owner-facing current_catalog projection supplies
    // the fresh snapshot-scoped IDs for a new confirmation.
    snapshot_id: 'snapshot-previous',
    asset_id: 'asset-orders-previous',
    aggregation: { operation: 'sum', column_id: 'column-amount-previous', column_name: 'amount' },
    fixed_filters: [{
      column_id: 'column-status-previous',
      operator: 'eq',
      value: { kind: 'text', value: 'paid' },
    }],
    default_date_column_id: 'column-created-at-previous',
    current_catalog: {
      snapshot_id: snapshotId,
      asset_id: 'asset-orders',
      schema_name: 'sales',
      table_name: 'orders',
      aggregation_column_id: 'column-amount',
      aggregation_column_name: 'amount',
      fixed_filters: [{
        column_id: 'column-order-id',
        operator: 'is_not_null',
      }],
      default_date_column_id: 'column-created-at',
      default_date_column_name: 'created_at',
      default_time_zone: 'UTC',
    },
  }
  const secondVersion = metric(2)
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/metrics`) return Promise.resolve({ items: [firstVersion], count: 1 })
    if (path === `/sources/${sourceId}/metrics/metric-revenue`) return Promise.resolve(firstVersion)
    if (path === `/sources/${sourceId}/metrics/metric-revenue/versions`) return Promise.resolve({ items: [firstVersion], count: 1 })
    throw new Error(`unexpected GET ${path}`)
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/metrics/metric-revenue/versions`) return Promise.resolve(secondVersion)
    throw new Error(`unexpected POST ${path}`)
  })
  renderPanel()

  fireEvent.click(screen.getByRole('button', { name: 'Manage metric definitions' }))
  await screen.findByText('Confirmed revenue')
  fireEvent.click(screen.getByRole('button', { name: 'Inspect definition' }))
  await screen.findByRole('button', { name: 'Create revised version' })
  fireEvent.click(screen.getByRole('button', { name: 'Create revised version' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Metric name' }), { target: { value: 'Confirmed revenue, revised' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /I confirm this is the intended reusable business definition/i }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm new version' }))

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/metrics/metric-revenue/versions`,
    expect.objectContaining({
      name: 'Confirmed revenue, revised',
      owner_confirmed: true,
      snapshot_id: snapshotId,
      asset_id: 'asset-orders',
      aggregation: { operation: 'sum', column_id: 'column-amount' },
      fixed_filters: [{ column_id: 'column-order-id', operator: 'is_not_null' }],
      unit_label: 'USD',
      default_date_column_id: 'column-created-at',
      default_time_zone: 'UTC',
    }),
    jwtOptions(),
  ))
  expect(await screen.findByText('A new immutable metric version was confirmed.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Create revised version' }))
  expect(await screen.findByRole('button', { name: 'Confirm new version' })).toBeTruthy()
})

it('maps metric-definition conflicts to a safe owner-facing message', async () => {
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/metrics`) {
      return Promise.reject(Object.assign(new Error('server detail'), {
        response: { error: { code: 'metric_definition_conflict' } },
      }))
    }
    throw new Error(`unexpected POST ${path}`)
  })
  renderPanel()

  fireEvent.click(screen.getByRole('button', { name: 'Manage metric definitions' }))
  await screen.findByText('No metric definitions have been confirmed for this source.')
  fireEvent.click(screen.getByRole('button', { name: 'New metric definition' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Metric name' }), { target: { value: 'Confirmed revenue' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /I confirm this is the intended reusable business definition/i }))
  fireEvent.click(screen.getByRole('button', { name: 'Save confirmed metric' }))

  expect((await screen.findByRole('alert')).textContent).toContain('A metric definition with that name already exists for this source.')
})

it('lets an owner re-confirm a needs-review metric against current columns without stale IDs', async () => {
  const staleMetric = {
    ...metric(),
    state: 'needs_review',
    needs_review_reason: 'column_changed',
    snapshot_id: 'snapshot-previous',
    asset_id: 'asset-orders-previous',
    aggregation: { operation: 'sum', column_id: 'column-amount-previous', column_name: 'amount' },
    fixed_filters: [{
      column_id: 'column-status-previous',
      operator: 'eq',
      value: { kind: 'text', value: 'paid' },
    }],
    default_date_column_id: 'column-created-at-previous',
    current_catalog: null,
  }
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/metrics`) return Promise.resolve({ items: [staleMetric], count: 1 })
    if (path === `/sources/${sourceId}/metrics/metric-revenue`) return Promise.resolve(staleMetric)
    if (path === `/sources/${sourceId}/metrics/metric-revenue/versions`) return Promise.resolve({ items: [staleMetric], count: 1 })
    throw new Error(`unexpected GET ${path}`)
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/metrics/metric-revenue/versions`) return Promise.resolve(metric(2))
    throw new Error(`unexpected POST ${path}`)
  })
  renderPanel()

  fireEvent.click(screen.getByRole('button', { name: 'Manage metric definitions' }))
  await screen.findByText('Confirmed revenue')
  fireEvent.click(screen.getByRole('button', { name: 'Inspect definition' }))
  const revise = await screen.findByRole('button', { name: 'Create revised version' })
  expect(revise.disabled).toBe(false)
  fireEvent.click(revise)
  expect(await screen.findByText(/Re-select its current columns and any fixed filters/i)).toBeTruthy()
  fireEvent.change(screen.getByRole('combobox', { name: 'Aggregate column' }), { target: { value: 'column-amount' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /I confirm this is the intended reusable business definition/i }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm new version' }))

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/metrics/metric-revenue/versions`,
    expect.objectContaining({
      owner_confirmed: true,
      snapshot_id: snapshotId,
      asset_id: 'asset-orders',
      aggregation: { operation: 'sum', column_id: 'column-amount' },
    }),
    jwtOptions(),
  ))
})
