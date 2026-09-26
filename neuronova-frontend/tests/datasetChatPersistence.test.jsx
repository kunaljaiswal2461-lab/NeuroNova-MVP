import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { useDataset } from '../src/context/DatasetContext'
import Chat from '../src/pages/Chat'

vi.mock('../src/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), getBlob: vi.fn(), streamSSE: vi.fn() },
}))

vi.mock('../src/context/DatasetContext', () => ({ useDataset: vi.fn() }))

vi.mock('../src/components/Layout', () => ({
  default: ({ children }) => <main>{children}</main>,
}))

const datasetId = 'dataset-1'
const sessionId = 'session-1'
const storageKey = `neuronova:chat-session:v1:${datasetId}`
const session = {
  session_id: sessionId,
  dataset_id: datasetId,
  mode: 'AUTO',
  message_count: 2,
}
const transcript = {
  messages: [
    { message_id: 'user-message', role: 'USER', content: 'Which column has the most missing values?' },
    {
      message_id: 'assistant-message',
      role: 'ASSISTANT',
      content: 'The revenue column has the most missing values.',
      mode_used: 'QUERY',
      intent: { mode: 'QUERY', confidence: 0.91, routed_by: 'heuristic' },
      citations: [],
      token_usage: { output_tokens: 12 },
    },
  ],
}

function renderChat() {
  return render(<MemoryRouter><Chat /></MemoryRouter>)
}

beforeEach(() => {
  window.sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  useDataset.mockReturnValue({
    activeDatasetId: datasetId,
    activeDataset: { dataset_id: datasetId, original_name: 'movies.csv', file_type: 'csv' },
  })
  api.get.mockImplementation(path => {
    if (path === `/chat/sessions/${sessionId}`) return Promise.resolve({ session })
    if (path === `/chat/sessions/${sessionId}/messages?limit=500`) return Promise.resolve(transcript)
    return Promise.resolve({ sessions: [] })
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('restores the saved dataset chat after the page is remounted', async () => {
  window.sessionStorage.setItem(storageKey, sessionId)

  const firstVisit = renderChat()
  await waitFor(() => expect(screen.getByText('The revenue column has the most missing values.')).toBeTruthy())
  expect(api.post).not.toHaveBeenCalled()

  firstVisit.unmount()
  renderChat()

  await waitFor(() => expect(screen.getByText('Which column has the most missing values?')).toBeTruthy())
  expect(api.get.mock.calls.filter(([path]) => path === `/chat/sessions/${sessionId}`)).toHaveLength(2)
  expect(api.post).not.toHaveBeenCalled()
})
