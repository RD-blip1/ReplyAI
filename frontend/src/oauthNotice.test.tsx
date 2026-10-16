/**
 * OAuth guidance and callback outcomes, through the whole App.
 *
 * The backend redirects the OAuth callback to `/?oauth=<result>` and reports whether its
 * OAuth client is configured on `/api/session`. Both were previously dropped by the UI, so
 * a declined or unconfigured connection was indistinguishable from clicking nothing. These
 * tests lock in that the guidance now reaches the screen.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const session = (overrides: Record<string, unknown> = {}) => ({
  csrf_token: 'csrf-token',
  oauth_configured: true,
  setup_message: null,
  connected: false,
  channel: null,
  ...overrides,
})

function stubSession(body: unknown) {
  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/session')) {
      return Promise.resolve(new Response(JSON.stringify(body), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    }
    return Promise.resolve(new Response('null', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  })
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState({}, '', '/')
})

describe('OAuth guidance through the app', () => {
  it('shows the backend setup message when OAuth is not configured', async () => {
    stubSession(session({
      oauth_configured: false,
      setup_message: 'Set GOOGLE_OAUTH_CLIENT_SECRETS_FILE to the OAuth client JSON file.',
    }))
    render(<App />)
    expect(await screen.findByText(/Set GOOGLE_OAUTH_CLIENT_SECRETS_FILE/)).toBeInTheDocument()
  })

  it('shows the callback outcome from the URL, then strips it', async () => {
    window.history.replaceState({}, '', '/?oauth=permission_denied')
    stubSession(session())
    render(<App />)
    expect(await screen.findByText(/You declined the extra YouTube permission/)).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toBe(''))
  })
})
