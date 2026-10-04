import { expect, it, vi } from 'vitest'
import { createApi } from '../src/api/request'

it('sends an authenticated JSON PUT without retrying or falling back to an API key', async () => {
  const storage = {
    getItem: vi.fn(key => key === 'access_token' ? 'owner-jwt' : null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  }
  const fetchImpl = vi.fn().mockResolvedValue(new Response(JSON.stringify({ profiling_enabled: true }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  }))
  const api = createApi({
    baseUrl: 'https://api.example.test/v1',
    fallbackApiKey: 'must-not-be-sent',
    storage,
    fetchImpl,
  })

  await expect(api.put('/sources/source-1/profiling', { profiling_enabled: true }, { requireJwt: true }))
    .resolves.toEqual({ profiling_enabled: true })

  expect(fetchImpl).toHaveBeenCalledTimes(1)
  expect(fetchImpl).toHaveBeenCalledWith(
    'https://api.example.test/v1/sources/source-1/profiling',
    expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ profiling_enabled: true }),
      headers: expect.objectContaining({
        Authorization: 'Bearer owner-jwt',
        'Content-Type': 'application/json',
      }),
    }),
  )
  expect(fetchImpl.mock.calls[0][1].headers['X-API-Key']).toBeUndefined()
})
