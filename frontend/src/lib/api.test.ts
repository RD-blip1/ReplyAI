import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiRequest } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('apiRequest error messages', () => {
  it('surfaces string FastAPI details such as session_required', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'session_expired' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    )))

    await expect(apiRequest('/api/youtube/publications')).rejects.toMatchObject({
      code: 'session_expired',
      message: 'session_expired',
    })
  })

  it('surfaces structured FastAPI error messages', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { code: 'history_unavailable', message: 'History is unavailable.' } }),
      { status: 503, headers: { 'Content-Type': 'application/json' } },
    )))

    await expect(apiRequest('/api/youtube/publications')).rejects.toMatchObject({
      code: 'history_unavailable',
      message: 'History is unavailable.',
    })
  })
})
