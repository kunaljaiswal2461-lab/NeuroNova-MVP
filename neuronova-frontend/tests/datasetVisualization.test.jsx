import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { useDataset } from '../src/context/DatasetContext'
import Visualization from '../src/pages/Visualization'

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

const visualization = {
  charts: [
    {
      chart_id: 'hist-revenue',
      type: 'HISTOGRAM',
      title: 'Distribution of Revenue',
      columns: ['revenue_usd'],
      semantic_context: 'FINANCIAL',
      config: {
        x_col: 'revenue_usd',
        x_label: 'Revenue',
        y_label: 'Frequency',
        bin_edges: [0, 50, 100],
        bin_counts: [8, 4],
        null_pct: 0,
        is_skewed: false,
        stats: { mean: 50, median: 45, std: 12, min: 0, max: 100, skew: 0.2, p25: 30, p75: 70 },
      },
    },
    {
      chart_id: 'bar-region',
      type: 'BAR',
      title: 'Value Distribution: Region',
      columns: ['region'],
      config: {
        x_col: 'region',
        x_label: 'Region',
        y_label: 'Count',
        cardinality: 2,
        top_values: [['North', 8], ['South', 4]],
      },
    },
  ],
  skipped_columns: [
    {
      column_name: 'transaction_id',
      intended_chart_type: 'HISTOGRAM',
      reason: 'Column is an identifier and not suitable for distribution charts.',
    },
    {
      column_name: 'metadata_json',
      intended_chart_type: 'BAR',
      reason: 'Column contains free-form text that does not fit a chart pattern.',
    },
  ],
  total_columns: 4,
  rendered_columns: 2,
}

beforeEach(() => {
  useDataset.mockReturnValue({
    activeDatasetId: 'dataset-1',
    activeDataset: { original_name: 'sample.csv' },
  })
  api.get.mockResolvedValue(visualization)
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('keeps the chart catalogue independently scrollable and preserves chart and skipped-column selection', async () => {
  render(<Visualization />)

  const workspace = await screen.findByRole('region', { name: 'Visualization workspace' })
  await waitFor(() => expect(screen.getByDisplayValue('revenue_usd').tagName).toBe('SELECT'))
  const catalogue = screen.getByRole('region', { name: 'Chart Catalogue' })
  expect(workspace.classList.contains('dot-grid')).toBe(true)
  expect(workspace.classList.contains('visualization-workspace')).toBe(true)
  expect(catalogue.classList.contains('visualization-catalogue-list')).toBe(true)
  expect(catalogue.closest('.visualization-side-rail')).not.toBeNull()

  expect(screen.getByRole('button', { name: /Distribution of Revenue/ })).toBeTruthy()
  expect(screen.getByRole('button', { name: /Value Distribution: Region/ })).toBeTruthy()
  expect(screen.getByText('Skipped / Data Quality')).toBeTruthy()
  expect(screen.queryByText('Chart cannot be generated')).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: /Value Distribution: Region/ }))
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Categorical' }).className).toContain('active')
    expect(screen.getByDisplayValue('region').tagName).toBe('SELECT')
  })

  fireEvent.click(screen.getByRole('button', { name: /metadata_json/ }))
  await waitFor(() => {
    expect(screen.getByDisplayValue('⊘ metadata_json').tagName).toBe('SELECT')
    expect(screen.getAllByText('Skipped / Data Quality')).toHaveLength(2)
  })

  fireEvent.click(screen.getByRole('button', { name: /transaction_id/ }))
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Numeric' }).className).toContain('active')
    expect(screen.getByDisplayValue('⊘ transaction_id').tagName).toBe('SELECT')
  })
})
