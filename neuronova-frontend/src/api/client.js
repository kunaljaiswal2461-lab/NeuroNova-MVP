const BASE_URL = 'http://localhost:8000/api/v1'
const API_KEY = 'dev-local-key-change-me'

function authHeaders() {
  return { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' }
}

async function get(path) {
  const res = await fetch(`${BASE_URL}${path}`, { headers: authHeaders() })
  if (!res.ok) {
    const msg = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${msg}`)
  }
  return res.json()
}

async function post(path, body = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const msg = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${msg}`)
  }
  return res.json()
}

async function postForm(path, formData) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'X-API-Key': API_KEY },
    body: formData,
  })
  if (!res.ok) {
    const msg = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${msg}`)
  }
  return res.json()
}

async function* streamSSE(path, body = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const msg = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${msg}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''
    for (const part of parts) {
      if (!part.trim()) continue
      const eventMatch = part.match(/^event:\s*(.+)$/m)
      const dataMatch = part.match(/^data:\s*(.+)$/ms)
      if (eventMatch && dataMatch) {
        try {
          yield { event: eventMatch[1].trim(), data: JSON.parse(dataMatch[1]) }
        } catch {
          // skip malformed frame
        }
      }
    }
  }
}

export const api = { get, post, postForm, streamSSE }
