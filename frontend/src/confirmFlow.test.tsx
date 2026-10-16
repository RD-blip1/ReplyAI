/**
 * The publish confirmation flow, exercised through the whole application rather than
 * through one component in isolation.
 *
 * `DraftPanel.test.tsx` mounts the draft panel directly, which is the right unit of
 * behaviour but leaves the part a person actually touches untested: getting from a real
 * comment to a real posted reply through the real App. This file mounts `App`, opens a
 * comment, generates and approves a draft, previews the publish, confirms it, and checks
 * the Posted state.
 *
 * Everything behind the browser is mocked at the fetch boundary, including the backend's
 * own call into YouTube. No request in this file can reach a real channel, a real model,
 * or the development database.
 *
 * This is still a jsdom test, not a browser test. It cannot see layout, focus rings, or
 * real key events. See `docs/DEVELOPMENT_LOG.md` for what that leaves unproven.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

// Synthetic identifiers with the same shape Google returns, so the assertions about a
// reply id being its parent's id plus "." still mean something. The real ids used in the
// live verification belong in the development log, not in a fixture.
const CHANNEL = 'UCtestchannel00000000a'
const VIDEO = 'TestVideoId01'
const PARENT = 'Ugx-testtoplevelcomment0'
const REPLY_TEXT =
  'Thank you for letting us know. I am sorry the video did not meet your expectations. ' +
  'We appreciate your feedback.'
const REPLY_ID = `${PARENT}.Abtestreplycommentid0000`

const SESSION = {
  csrf_token: 'csrf-token', oauth_configured: true, setup_message: null,
  connected: true,
  channel: {
    channel_id: CHANNEL, channel_title: 'Test Channel', channel_thumbnail_url: null,
    reconnect_required: false, comments_scope_available: true,
  },
}
const VIDEO_PAGE = {
  items: [{
    video_id: VIDEO, title: 'A video', thumbnail_url: null,
    published_at: '2026-09-20T00:00:00Z', watch_url: `https://youtu.be/${VIDEO}`,
  }],
  next_page_token: null,
}
const COMMENT_PAGE = {
  items: [{
    comment_id: PARENT, author_name: 'A viewer', author_thumbnail_url: null,
    published_at: '2026-09-21T00:00:00Z', text: 'The batching change helped a lot.',
    total_reply_count: 0,
  }],
  next_page_token: null,
}
const DRAFT = {
  id: 'draft-1', text: REPLY_TEXT, revision: 2, original_comment: 'A comment',
  video_id: VIDEO, comment_id: PARENT,
  primary_classification: { sentiment: 'positive', intent: 'praise' },
  secondary_classification: { sentiment: 'positive', intent: 'praise' },
  agreement: 'agree', generation_status: 'complete', review_status: 'complete',
  human_decision: 'pending', quality: null, failure: {}, providers: {}, token_usage: {},
}
const QUALITY = {
  relevance: 'pass', unsupported_claims: 'pass', inappropriate_content: 'pass',
  findings: [],
}
const REVIEWED = { ...DRAFT, quality: QUALITY }
const APPROVED = { ...REVIEWED, human_decision: 'approved' }
const ELIGIBILITY = {
  eligible: true, blocked_code: null, blocked_message: null, scope_available: true,
  channel_title: 'Test Channel', revision: 2,
  review: { review_status: 'complete', checks: QUALITY, findings: [], agreement: 'agree' },
}
const POSTED = {
  id: 'pub-1', operation_id: 'operation-1', status: 'posted', channel_id: CHANNEL,
  video_id: VIDEO, parent_comment_id: PARENT, draft_revision: 2, text: REPLY_TEXT,
  reply_comment_id: REPLY_ID, failure: {},
  sent_at: '2026-09-28T08:19:00Z', created_at: '2026-09-28T08:19:00Z',
  resolved_at: '2026-09-28T08:19:01Z', retryable: false,
}

const USAGE = {
  entries: [], totals: {
    requests: 0, reported_requests: 0, unknown_requests: 0,
    discarded_requests: 0, input_tokens: 0, output_tokens: 0,
  },
  coverage: {
    complete: true, pre_ledger_activity: false, unbacked_snapshot_stages: [],
    notice: null, unknown_notice: null,
  },
}

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' } })
const problem = (status: number, code: string, message: string) =>
  new Response(JSON.stringify({ detail: { code, message } }), {
    status, headers: { 'Content-Type': 'application/json' } })

type Route = (url: string, init: RequestInit) => Response | Promise<Response>

/**
 * Mount the whole app against a scripted backend.
 *
 * `publish` decides what the backend does when the user confirms, which is how each
 * outcome is exercised without ever touching YouTube. The state it writes into `stored`
 * is what later reads return, so the panel has to learn the outcome the way it would
 * after a refresh rather than being handed the answer directly.
 */
function mountApp(route: Route) {
  const calls = { publishes: 0, bodies: [] as string[] }
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init: RequestInit = {}) =>
    route(String(input), init))
  return { calls, view: render(<App />) }
}

/**
 * Walk from the workspace to an approved, publishable draft.
 *
 * Everything before the publish decision is the ordinary product path, so it is driven
 * through the interface rather than injected, which is the point of this file.
 */
async function reachApprovedDraft(user: ReturnType<typeof userEvent.setup>) {
  // The channel name shows in more than one place, so wait for all of them to settle.
  await screen.findAllByText('Test Channel')
  await user.click(await screen.findByRole('button', { name: /A video/i }))
  await user.click(await screen.findByText(/A viewer/i))
  await screen.findByRole('textbox', { name: /reply text/i })
}

/** The happy-path backend: everything is already reviewed and approved. */
function happyBackend(publish: () => Response | Promise<Response>, calls: {
  publishes: number; bodies: string[]
}, stored: { publication: unknown }) {
  const draft = APPROVED
  return async (url: string, init: RequestInit): Promise<Response> => {
    const path = url.replace('http://127.0.0.1:8001', '')
    if (path.startsWith('/api/session')) return ok(SESSION)
    if (path.startsWith('/health') || path.startsWith('/ready')) return ok({ status: 'ok' })
    // The panel reads both of these on open and renders from their shapes, so they have
    // to be answered properly rather than falling through to some other body.
    if (path.endsWith('/setup')) return ok({ configured: true, missing: [], invalid: [] })
    if (path.endsWith('/usage')) return ok(USAGE)
    if (path.startsWith('/api/youtube/videos/') && path.includes('/comments')) {
      return ok(COMMENT_PAGE)
    }
    if (path.startsWith('/api/youtube/videos')) return ok(VIDEO_PAGE)
    if (path.includes('/publication')) {
      if (init.method === 'POST') {
        calls.publishes += 1
        calls.bodies.push(String(init.body))
        const response = await publish()
        if (response.ok) stored.publication = POSTED
        return response
      }
      return ok({ publication: stored.publication, eligibility: ELIGIBILITY })
    }
    if (path.endsWith('/conversation')) return ok({ turns: [] })
    if (path.includes('?video_id')) return ok(draft)
    return ok(draft)
  }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('publishing confirmation, through the whole application', () => {
  it('goes from a comment to a posted reply, and sends one request with the saved revision', async () => {
    const user = userEvent.setup()
    const calls = { publishes: 0, bodies: [] as string[] }
    const stored: { publication: unknown } = { publication: null }
    mountApp(happyBackend(() => ok(POSTED), calls, stored))

    await reachApprovedDraft(user)

    await user.click(screen.getByRole('button', { name: /post reply to youtube/i }))
    const dialog = screen.getByRole('dialog')
    // The confirmation must name the real target before anything is sent.
    expect(within(dialog).getByText('Test Channel')).toBeInTheDocument()
    expect(within(dialog).getByText(VIDEO)).toBeInTheDocument()
    expect(within(dialog).getByText(PARENT)).toBeInTheDocument()
    expect(calls.publishes).toBe(0)

    await user.click(within(dialog).getByRole('button', { name: /confirm & post/i }))

    expect(await screen.findByText('Posted to YouTube')).toBeInTheDocument()
    expect(screen.getByText(new RegExp(REPLY_ID.replace(/\./g, '\\.')))).toBeInTheDocument()
    expect(calls.publishes).toBe(1)
    expect(JSON.parse(calls.bodies[0])).toEqual({ revision: 2, text: REPLY_TEXT })
    // A posted reply cannot be offered again.
    expect(screen.queryByRole('button', { name: /post reply to youtube/i })).not.toBeInTheDocument()
  })

  it('sends nothing when the confirmation is cancelled', async () => {
    const user = userEvent.setup()
    const calls = { publishes: 0, bodies: [] as string[] }
    const stored: { publication: unknown } = { publication: null }
    mountApp(happyBackend(() => ok(POSTED), calls, stored))

    await reachApprovedDraft(user)
    await user.click(screen.getByRole('button', { name: /post reply to youtube/i }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /cancel/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(calls.publishes).toBe(0)
    // The post control is still there, because nothing was published.
    expect(screen.getByRole('button', { name: /post reply to youtube/i })).toBeEnabled()
  })

  it('sends one request when confirm is clicked repeatedly while the first is in flight', async () => {
    const user = userEvent.setup()
    const calls = { publishes: 0, bodies: [] as string[] }
    const stored: { publication: unknown } = { publication: null }
    let release = () => {}
    const pending = new Promise<Response>((resolve) => { release = () => resolve(ok(POSTED)) })
    mountApp(happyBackend(() => pending, calls, stored))

    await reachApprovedDraft(user)
    await user.click(screen.getByRole('button', { name: /post reply to youtube/i }))
    const confirm = within(screen.getByRole('dialog')).getByRole('button', { name: /confirm & post/i })

    await user.click(confirm)
    await user.click(confirm)
    await user.click(confirm)

    expect(calls.publishes).toBe(1)
    expect(confirm).toBeDisabled()
    release()
    expect(await screen.findByText('Posted to YouTube')).toBeInTheDocument()
    expect(calls.publishes).toBe(1)
  })

  it('shows the durable unknown state when the request never answers, and offers no retry', async () => {
    const user = userEvent.setup()
    const calls = { publishes: 0, bodies: [] as string[] }
    // The backend stored an attempt whose outcome it could not determine.
    const unknown = {
      ...POSTED, status: 'unknown', reply_comment_id: null, retryable: false,
      resolved_at: null,
    }
    const stored: { publication: unknown } = { publication: null }
    mountApp(async (url, init) => {
      if (url.includes('/publication') && init.method === 'POST') {
        calls.publishes += 1
        stored.publication = unknown
        return Promise.reject(new TypeError('Failed to fetch')) as unknown as Response
      }
      return happyBackend(() => ok(POSTED), calls, stored)(url, init)
    })

    await reachApprovedDraft(user)
    await user.click(screen.getByRole('button', { name: /post reply to youtube/i }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /confirm & post/i }))

    // The client's guess is discarded in favour of what the server actually recorded.
    expect(await screen.findByText(/outcome unknown/i)).toBeInTheDocument()
    expect(screen.getByText(/will not send it again/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /post reply to youtube/i })).not.toBeInTheDocument()
    expect(calls.publishes).toBe(1)
  })

  it('moves focus into the dialog, closes on Escape, and restores focus to the trigger', async () => {
    const user = userEvent.setup()
    const calls = { publishes: 0, bodies: [] as string[] }
    const stored: { publication: unknown } = { publication: null }
    mountApp(happyBackend(() => ok(POSTED), calls, stored))

    await reachApprovedDraft(user)
    const trigger = screen.getByRole('button', { name: /post reply to youtube/i })
    await user.click(trigger)

    // Focus must be inside the dialog, not left behind on the button that opened it.
    const dialog = screen.getByRole('dialog')
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    expect(within(dialog).getByRole('button', { name: /cancel/i })).toHaveFocus()

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(calls.publishes).toBe(0)
    expect(trigger).toHaveFocus()

    // Cancel has to restore focus as well, so the dialog is escapable both ways.
    await user.click(trigger)
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /cancel/i }))
await waitFor(() => expect(trigger).toHaveFocus())
    expect(calls.publishes).toBe(0)
  })

  it('shows a pre-send refusal in the dialog and keeps the reply editable', async () => {
    const user = userEvent.setup()
    const calls = { publishes: 0, bodies: [] as string[] }
    const stored: { publication: unknown } = { publication: null }
    mountApp(happyBackend(
      () => problem(403, 'comments_disabled', 'Replies are turned off on this comment.'),
      calls, stored))

    await reachApprovedDraft(user)
    await user.click(screen.getByRole('button', { name: /post reply to youtube/i }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /confirm & post/i }))

    // No record exists, so the server's own message is the only thing to show. It is shown
    // in the dialog next to the refused text, and on the panel behind it.
    await waitFor(() =>
      expect(screen.getAllByText(/replies are turned off/i).length).toBeGreaterThan(0))
    expect(calls.publishes).toBe(1)
    // The dialog stays open so the reason is read next to the text that was refused.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})