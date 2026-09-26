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
  expect(emailLink.getAttribute('href')).toBe('mailto:hello@tryneuronova.in')
  expect(container.querySelector('.landing-cta-contact-links')?.contains(xLink)).toBe(true)
})
