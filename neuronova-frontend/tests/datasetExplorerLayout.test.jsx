import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { useDataset } from '../src/context/DatasetContext'
import Explorer from '../src/pages/Explorer'

vi.mock('../src/api/client', () => ({ api: { get: vi.fn() } }))
vi.mock('../src/context/DatasetContext', () => ({ useDataset: vi.fn() }))
vi.mock('../src/components/Layout', () => ({
  default: ({ actions, children, className }) => (
    <div className={className}>
      <div>{actions}</div>
      <main className="page-area">{children}</main>
    </div>
  ),
}))

const profile = {
  schema: {
    row_count: 15500,
    col_count: 2,
    columns: [
      { name: 'transaction_id', dtype: 'string' },
      { name: 'region', dtype: 'string' },
    ],
  },
  semantic: {
    columns: [
      { name: 'transaction_id', semantic_type: 'IDENTIFIER' },
      { name: 'region', semantic_type: 'GEOGRAPHIC' },
    ],
  },
  quality: {
    columns: [
      { name: 'transaction_id', null_pct: 0 },
      { name: 'region', null_pct: 0 },
    ],
    duplicate_row_pct: 0,
  },
  stats: {
    columns: [
      { name: 'transaction_id', categorical: { cardinality: 15500 } },
      { name: 'region', categorical: { cardinality: 7 } },
    ],
  },
  health: {
    score: 92,
    grade: 'A',
    components: { completeness: 100, uniqueness: 100, consistency: 100, cleanliness: 73 },
  },
}

beforeEach(() => {
  useDataset.mockReturnValue({
    activeDatasetId: 'dataset-1',
    activeDataset: { original_name: 'sample.csv' },
    updateDataset: vi.fn(),
  })
  api.get.mockImplementation(url => {
    if (url.includes('/profile')) return Promise.resolve(profile)
    if (url.includes('/findings')) return Promise.resolve({ findings: [{ severity: 'HIGH', title: 'Skewed distribution', column: 'region' }] })
    return Promise.resolve({ skipped_columns: [{ column_name: 'transaction_id' }] })
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('keeps the schema and dataset-information regions independent without changing table search', async () => {
  const view = render(<Explorer />)

  await screen.findByText('transaction_id')
  const workspace = view.container.querySelector('.explorer-workspace')
  const schemaCard = view.container.querySelector('.explorer-schema-card')
  const tableScroll = screen.getByRole('region', { name: 'Schema Overview table' })
  const sideRail = view.container.querySelector('.explorer-side-rail')

  expect(workspace).not.toBeNull()
  expect(schemaCard).not.toBeNull()
  expect(tableScroll.classList.contains('explorer-table-scroll')).toBe(true)
  expect(sideRail).not.toBeNull()
  expect(tableScroll.querySelector('.explorer-schema-table')).not.toBeNull()
  expect(screen.getAllByRole('columnheader')).toHaveLength(7)
  expect(screen.getByText('Dataset Health')).toBeTruthy()
  expect(screen.getByText('High-Severity Flags')).toBeTruthy()
  expect(screen.getByText('Dataset Stats')).toBeTruthy()

  fireEvent.change(screen.getByPlaceholderText('Search columns...'), { target: { value: 'region' } })
  await waitFor(() => {
    expect(screen.getByText('region')).toBeTruthy()
    expect(screen.queryByText('transaction_id')).toBeNull()
  })
})
