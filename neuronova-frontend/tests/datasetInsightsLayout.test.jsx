import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { useDataset } from '../src/context/DatasetContext'
import Insights from '../src/pages/Insights'

const navigate = vi.fn()

vi.mock('../src/api/client', () => ({ api: { get: vi.fn() } }))
vi.mock('../src/context/DatasetContext', () => ({ useDataset: vi.fn() }))
vi.mock('react-router-dom', () => ({ useNavigate: () => navigate }))
vi.mock('../src/components/Layout', () => ({
  default: ({ actions, children, className }) => (
    <div className={className}>
      <header className="topbar">AI Insights</header>
      <div>{actions}</div>
      <main className="page-area">{children}</main>
    </div>
  ),
}))

const insights = {
  model_used: 'gpt-4o',
  executive_summary: {
    headline: 'Dataset is robust with high health score but requires attention to outliers.',
    overview: 'The dataset contains several useful analytical signals.',
    key_strengths: ['High health score', 'No duplicate rows'],
    key_concerns: ['High outlier percentages'],
    recommended_next_steps: ['Review the outlier treatment strategy.', 'Validate the affected columns.'],
  },
  finding_explanations: [{
    finding_id: 'high-1',
    title: 'Skewed distribution in revenue_usd',
    plain_english: 'Revenue is skewed by a small number of large values.',
    suggested_action: 'Consider a log transform before modelling.',
  }],
  suggested_questions: [{ question: 'What is the average revenue by region?', intent: 'QUERY' }],
}

beforeEach(() => {
  useDataset.mockReturnValue({
    activeDatasetId: 'dataset-1',
    activeDataset: { original_name: 'sample.csv' },
  })
  api.get.mockImplementation(url => {
    if (url.endsWith('/insights')) return Promise.resolve(insights)
    if (url.includes('severity=HIGH')) {
      return Promise.resolve({ findings: [{
        id: 'high-1',
        title: 'Skewed distribution in revenue_usd',
        column: 'revenue_usd',
        evidence: { skew: 20.51, median: 2530.99 },
        confidence: 1,
      }] })
    }
    return Promise.resolve({ findings: [{
      id: 'medium-1',
      title: 'High outlier density',
      column: 'revenue_usd',
      evidence: { outlier_pct: 12.9 },
    }] })
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('uses the full Insights workspace while preserving findings and follow-up navigation', async () => {
  const view = render(<Insights />)

  await screen.findByText('Dataset is robust with high health score but requires attention to outliers.')

  expect(view.container.querySelector('.insights-page')).not.toBeNull()
  expect(view.container.querySelector('.insights-workspace')).not.toBeNull()
  expect(view.container.querySelector('.insights-executive-card')).not.toBeNull()
  expect(view.container.querySelector('.insights-tags')).not.toBeNull()
  expect(view.container.querySelector('.insight-finding-high')).not.toBeNull()
  expect(view.container.querySelector('.insight-finding-warning')).not.toBeNull()
  expect(view.container.querySelectorAll('.insight-evidence-chip')).toHaveLength(4)
  expect(view.container.querySelector('.insights-follow-up-card')).not.toBeNull()

  fireEvent.click(screen.getByRole('button', { name: /What is the average revenue by region/i }))
  await waitFor(() => {
    expect(navigate).toHaveBeenCalledWith('/chat', {
      state: { prefill: 'What is the average revenue by region?' },
    })
  })
})
