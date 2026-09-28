import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useAuth } from '../src/context/AuthContext'
import Landing from '../src/pages/Landing'

vi.mock('../src/context/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('../src/components/landing/DatasetExplorerDemo', () => ({
  default: () => <div data-testid="dataset-explorer-demo" />,
}))

beforeEach(() => {
  useAuth.mockReturnValue({ user: null, loading: false })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('places X, LinkedIn, and email contact links in the landing CTA', () => {
  const { container } = render(<MemoryRouter><Landing /></MemoryRouter>)

  const xLink = screen.getByRole('link', { name: 'Follow NeuroNova on X' })
  const linkedinLink = screen.getByRole('link', { name: 'Follow NeuroNova on LinkedIn' })
  const emailLink = screen.getByRole('link', { name: 'Email hello@tryneuronova.in' })

  expect(xLink.getAttribute('href')).toBe('https://x.com/TeamNeuroNova')
  expect(linkedinLink.getAttribute('href')).toBe('https://www.linkedin.com/company/neuronova-official/?viewAsMember=true')
  expect(emailLink.getAttribute('href')).toBe('mailto:hello@tryneuronova.in?subject=NeuroNova%20Inquiry')
  expect(xLink.textContent).toBe('')
  expect(linkedinLink.textContent).toBe('')
  expect(container.querySelector('.landing-cta-contact-links')?.contains(xLink)).toBe(true)
})

it('links each technology in the powered-by strip to its documentation', () => {
  render(<MemoryRouter><Landing /></MemoryRouter>)

  expect(screen.getByRole('link', { name: 'Read LangChain documentation' }).getAttribute('href')).toBe('https://docs.langchain.com/oss/python/langchain/overview')
  expect(screen.getByRole('link', { name: 'Read LangGraph documentation' }).getAttribute('href')).toBe('https://docs.langchain.com/oss/python/langgraph/overview')
  expect(screen.getByRole('link', { name: 'Read OpenAI documentation' }).getAttribute('href')).toBe('https://platform.openai.com/docs/quickstart/make-your-first-api-request')
  expect(screen.getByRole('link', { name: 'Read pgvector documentation' }).getAttribute('href')).toBe('https://github.com/pgvector/pgvector')
  expect(screen.getByRole('link', { name: 'Read Python documentation' }).getAttribute('href')).toBe('https://docs.python.org/3/')
})
