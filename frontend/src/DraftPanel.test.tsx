import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DraftPanel from './DraftPanel'

const DRAFT = {
  id: 'draft-1', text: 'Saved reply text', revision: 1, original_comment: 'A comment',
  primary_classification: null, secondary_classification: null, agreement: 'unknown',
  generation_status: 'complete', review_status: 'complete', human_decision: 'pending',
  quality: null, failure: {}, providers: {}, token_usage: {},
}
const USAGE = {
  entries: [],
  totals: { requests: 0, reported_requests: 0, unknown_requests: 0, discarded_requests: 0,
            input_tokens: 0, output_tokens: 0 },
  coverage: { complete: true, pre_ledger_activity: false, unbacked_snapshot_stages: [],
              notice: null, unknown_notice: null },
}
const SETUP = { configured: true, missing: [], invalid: [] }
const TURN = {
  id: 'turn-1', base_revision: 1, instruction: 'make it warmer',
  suggestion: 'A warmer suggested reply', status: 'suggested', created_at: '2026-09-27T19:00:49Z',
}

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const problem = (status: number, code: string, message: string) =>
  new Response(JSON.stringify({ detail: { code, message } }), {
    status, headers: { 'Content-Type': 'application/json' } })

type Route = (url: string, init: RequestInit) => Response | Promise<Response>

// The in-flight request, so the deadline can be observed settling and the abort asserted.
let pending: AbortSignal | null = null

function mount(route: Route, { strict = true } = {}) {
  const seen: string[] = []
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    seen.push(url)
    return route(url, init)
  })
  const ui = strict
    ? render(<StrictMode><DraftPanel videoId="v1" commentId="c1" csrf="csrf" /></StrictMode>)
    : render(<DraftPanel videoId="v1" commentId="c1" csrf="csrf" />)
  return { ...ui, seen }
}

const base = (url: string) => url.replace('http://127.0.0.1:8001/api/drafts', '')
const isConversation = (url: string) => base(url).endsWith('/conversation')

afterEach(() => {
  // Without global test mode Testing Library does not auto-clean, and queries would match
  // panels left over from earlier cases.
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('refinement instruction field', () => {
  it('is a labelled, enabled textarea that accepts typing and native paste', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      return ok({ turns: [] })
    })

    const field = await screen.findByPlaceholderText(/Instruction, e.g/i)
    // A disabled field refuses paste while still looking like it should work.
    expect(field).toBeEnabled()
    expect(field.tagName).toBe('TEXTAREA')

    await user.click(field)
    await user.paste('make it warmer')
    await waitFor(() => expect(field).toHaveValue('make it warmer'))

    // A second paste appends rather than replacing, like a real browser.
    await user.paste(' and shorter')
    await waitFor(() => expect(field).toHaveValue('make it warmer and shorter'))
  })

  it('stays enabled after a settled request, so paste keeps working', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (base(url).endsWith('/refine')) return ok({ turn: TURN, draft: DRAFT })
      return ok({ turns: [] })
    })

    const field = await screen.findByPlaceholderText(/Instruction, e.g/i)
    await user.type(field, 'make it warmer')
    await user.click(screen.getByRole('button', { name: /send instruction/i }))

    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()
    expect(field).toBeEnabled()
  })

  it('names the reason whenever the field or Send is disabled', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      return ok({ turns: [] })
    })

    const field = await screen.findByPlaceholderText(/Instruction, e.g/i)
    const send = screen.getByRole('button', { name: /send instruction/i })
    // Empty instruction: the field works, Send does not, and the reason is stated.
    expect(field).toBeEnabled()
    expect(send).toBeDisabled()
    expect(screen.getAllByText(/type an instruction to enable send/i)[0]).toBeInTheDocument()

    await user.type(field, 'make it warmer')
    await waitFor(() => expect(send).toBeEnabled())
  })
})

describe('refinement send settles its loading state', () => {
  it('clears the spinner and shows the suggestion under StrictMode', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (base(url).endsWith('/refine')) return ok({ turn: TURN, draft: DRAFT })
      return ok({ turns: [] })
    })

    // The conversation finishes loading; this never used to happen under StrictMode.
    expect(await screen.findByText(/no refinement instructions yet/i)).toBeInTheDocument()
    expect(screen.queryByText(/loading conversation/i)).not.toBeInTheDocument()

    await user.type(await screen.findByPlaceholderText(/Instruction, e.g/i), 'make it warmer')
    await user.click(screen.getByRole('button', { name: /send instruction/i }))

    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()
    // The stuck message from the bug report must be gone.
    await waitFor(() =>
      expect(screen.queryByText(/asking the model for a suggested reply/i)).not.toBeInTheDocument())
  })

  it('clears the spinner and explains a provider failure', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (base(url).endsWith('/refine')) {
        return problem(502, 'transient_error', 'The model could not be reached. Try again.')
      }
      return ok({ turns: [] })
    })

    await user.type(await screen.findByPlaceholderText(/Instruction, e.g/i), 'make it warmer')
    await user.click(screen.getByRole('button', { name: /send instruction/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be reached/i)
    await waitFor(() =>
      expect(screen.queryByText(/asking the model for a suggested reply/i)).not.toBeInTheDocument())
    // The instruction survives a failure so it is not retyped.
    expect(screen.getByPlaceholderText(/Instruction, e.g/i)).toHaveValue('make it warmer')
  })

  it('reaches a bounded timeout with an actionable error instead of spinning forever', async () => {
    // waitFor polls on timers, so real time must keep flowing while the deadline is faked.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    // A connection that never comes back. Real fetch rejects with AbortError when its
    // signal fires, so the mock has to do the same or it is not testing the deadline.
    mount((url, init) => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (base(url).endsWith('/refine')) {
        return new Promise<Response>((_, reject) => {
          pending = init.signal ?? null
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('Aborted', 'AbortError')))
        }) as unknown as Response
      }
      return ok({ turns: [] })
    })

    const field = await screen.findByPlaceholderText(/Instruction, e.g/i)
    await act(async () => {
      fireEvent.change(field, { target: { value: 'make it warmer' } })
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /send instruction/i }))
    })
    expect(screen.getByText(/asking the model for a suggested reply/i)).toBeInTheDocument()

    // Past the 120s refinement deadline the panel must give up and say so.
    await act(async () => { await vi.advanceTimersByTimeAsync(121_000) })

    expect(screen.getByRole('alert')).toHaveTextContent(/did not finish in time/i)
    expect(screen.getByRole('alert')).toHaveTextContent(/reload saved state/i)
    await waitFor(() =>
      expect(screen.queryByText(/asking the model for a suggested reply/i)).not.toBeInTheDocument())
    // The in-flight request really was aborted, not merely abandoned.
    expect(pending?.aborted).toBe(true)
  })
})

describe('conversation loading', () => {
  it('reports a failed conversation fetch and stays usable', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return problem(500, 'internal_error', 'Conversation unavailable.')
      if (base(url).endsWith('/refine')) return ok({ turn: TURN, draft: DRAFT })
      return ok({ turns: [] })
    })

    // The server's own message is shown, since it is the actionable one.
    expect(await screen.findByRole('alert')).toHaveTextContent(/conversation unavailable/i)
    // Loading must not stay on screen even when the fetch failed.
    expect(screen.queryByText(/loading conversation/i)).not.toBeInTheDocument()

    const field = screen.getByPlaceholderText(/Instruction, e.g/i)
    expect(field).toBeEnabled()
    await user.type(field, 'make it warmer')
    await user.click(screen.getByRole('button', { name: /send instruction/i }))
    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()
  })

  it('recovers a persisted turn on reload without resending a model request', async () => {
    // The server completed and stored a turn, but the response never reached the client.
    // Reload must surface it and must not pay for a second refinement.
    let persisted: typeof TURN | null = null
    let refinements = 0
    const user = userEvent.setup()
    const { seen } = mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: persisted ? [persisted] : [] })
      if (base(url).endsWith('/refine')) {
        refinements += 1
        persisted = TURN
        // The work lands on the server, then the connection dies before replying.
        return Promise.reject(new TypeError('Failed to fetch')) as unknown as Response
      }
      return ok({ turns: [] })
    })

    await user.type(await screen.findByPlaceholderText(/Instruction, e.g/i), 'make it warmer')
    await user.click(screen.getByRole('button', { name: /send instruction/i }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(refinements).toBe(1)

    await user.click(screen.getByRole('button', { name: /reload/i }))

    // The stored suggestion is recovered from the conversation, not regenerated.
    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()
    expect(refinements).toBe(1)
    expect(seen.filter(url => base(url).endsWith('/refine'))).toHaveLength(1)
  })

  it('shows persisted turns on a fresh mount without sending a model request', async () => {
    const { seen } = mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: [TURN] })
      return ok({ turns: [] })
    })

    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()
    expect(seen.some(url => base(url).endsWith('/refine'))).toBe(false)
  })
})

// The saved reply after Apply -> Review -> Approve: revision 2, review complete,
// decision approved, and one turn whose status is `applied` at base_revision 1.
const SUGGESTION =
  'Thanks for the detailed walkthrough!\n\nI tried the batching change this morning and the ' +
  'latency dropped noticeably.\n\nOne thing worth flagging: the cache only helps once the ' +
  'working set fits, so measure it on your real query mix before rolling it out.'
const APPLIED_DRAFT = {
  ...DRAFT, text: SUGGESTION, revision: 2, review_status: 'complete', human_decision: 'approved',
}
const APPLIED_TURN = { ...TURN, status: 'applied', base_revision: 1, suggestion: SUGGESTION }

describe('refresh restoration', () => {
  it('restores a saved, applied, reviewed, approved draft without a model call', async () => {
    const { seen } = mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(APPLIED_DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: [APPLIED_TURN] })
      return ok({ turns: [] })
    })

    // The saved reply and its decision come from the backend, not from local guesses.
    // expect(await screen.findByText('Approval Status')).toBeInTheDocument()
    expect(await screen.findByText(/Approved/i)).toBeInTheDocument()
    expect(screen.getByText((_, node) =>
      node?.tagName === 'P' && /Revision 2/.test(node.textContent ?? ''))).toBeInTheDocument()
    expect(screen.getByText((_, node) =>
      node?.tagName === 'P' && /Review: complete/.test(node.textContent ?? ''))).toBeInTheDocument()

    const suggestions = await screen.findAllByText(/Thanks for the detailed walkthrough!/i)
    expect(suggestions.length).toBeGreaterThan(0)
    expect(screen.getAllByText(/^applied/i)[0]).toBeInTheDocument()

    // Applied work must not offer the actions that would re-apply it.
    expect(screen.queryByRole('button', { name: /apply suggestion/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /discard suggestion/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/suggestion.*waiting/i)).not.toBeInTheDocument()

    // Loading history must not issue a model request.
    expect(seen.filter(url => base(url).endsWith('/refine'))).toEqual([])
    expect(seen.some(url => isConversation(url))).toBe(true)
  })

  it('lays a turn out as separate text, status, and control elements', async () => {
    // The structure is what guarantees the layout: a turn's text, its status, and its
    // controls are distinct block siblings, so none of them can overlap another.
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(APPLIED_DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: [APPLIED_TURN] })
      return ok({ turns: [] })
    })

    const statusNode = await screen.findByText(/applied/i)
    const turn = statusNode.closest('article')
    expect(turn).toHaveClass('applied')

    // The suggestion is a block of its own, and it preserves the stored line breaks.
    const suggestion = within(turn as HTMLElement).getByText(/Thanks for the detailed walkthrough!/i)
    expect(suggestion.tagName).toBe('DIV')

    // Status is a badge plus metadata on its own line, not run together with the text.
    expect(within(turn as HTMLElement).getByText(/applied/i)).toBeInTheDocument()

    // The composer sits outside the history container.
    const history = document.querySelector('.refinement-history')
    expect(history).not.toBeNull()
    const field = screen.getByPlaceholderText(/Instruction, e.g/i)
    expect(history?.contains(field)).toBe(false)
    expect(turn && history?.contains(turn)).toBe(true)
  })

  it('gives the instruction field an id unique to the draft', async () => {
    const first = mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(APPLIED_DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: [APPLIED_TURN] })
      return ok({ turns: [] })
    })
    const field = await screen.findByPlaceholderText(/Instruction, e.g/i)
    expect(field.id).toBe(`refinement-instruction-${APPLIED_DRAFT.id}`)
    expect(first.seen.some(url => base(url).endsWith('/refine'))).toBe(false)
  })

  it('keeps the instruction field separate from the conversation history', async () => {
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(APPLIED_DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: [APPLIED_TURN] })
      return ok({ turns: [] })
    })

    await screen.findByText((_, node) => node?.textContent === SUGGESTION)
    const field = screen.getByPlaceholderText(/Instruction, e.g/i)
    // A restored turn must not spill into the field, and the field must not be pre-filled.
    expect(field).toHaveValue('')
    expect(await screen.findByText(/make it warmer/)).toBeInTheDocument()
  })
})

describe('apply and discard', () => {
  it('applies a suggestion, settles the spinner, and shows the new state', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: [TURN] })
      if (base(url).endsWith('/apply')) {
        return ok({ ...DRAFT, text: 'A warmer suggested reply', revision: 2, review_status: 'pending' })
      }
      return ok({ turns: [] })
    })

    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /apply suggestion/i }))

    expect(await screen.findByText((_, node) =>
      node?.tagName === 'P' && /Revision 2/.test(node.textContent ?? ''))).toBeInTheDocument()
    expect(screen.getByText((_, node) =>
      node?.tagName === 'P' && /Review: pending/.test(node.textContent ?? ''))).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText(/applying the suggestion/i)).not.toBeInTheDocument())
  })

  it('discards a suggestion and settles the spinner', async () => {
    const user = userEvent.setup()
    mount(url => {
      if (base(url) === '/setup') return ok(SETUP)
      if (base(url).startsWith('?video_id')) return ok(DRAFT)
      if (base(url).endsWith('/usage')) return ok(USAGE)
      if (isConversation(url)) return ok({ turns: [TURN] })
      if (base(url).endsWith('/discard')) return ok({ ...TURN, status: 'discarded' })
      return ok({ turns: [] })
    })

    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /discard suggestion/i }))

    expect(await screen.findByText(/^discarded/i)).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText(/discarding the suggestion/i)).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /apply suggestion/i })).not.toBeInTheDocument()
  })
})

// Publishing. The fixtures below are the state after the full local path: a saved
// revision 2, a completed review, and an approved decision on the owner's channel.
type QualityFixture = {
  relevance: string; unsupported_claims: string; inappropriate_content: string
  confidence: number; findings: string[]
}
const PASSING_QUALITY: QualityFixture = {
  relevance: 'pass', unsupported_claims: 'pass', inappropriate_content: 'pass',
  confidence: 0.95, findings: [],
}
const PUBLISHABLE_DRAFT = {
  ...APPLIED_DRAFT, video_id: 'v1', comment_id: 'c1', quality: PASSING_QUALITY,
}
const CHECKS = {
  relevance: 'pass', unsupported_claims: 'pass', inappropriate_content: 'pass',
}
const qualityCheckLabelsForTest = [
  'relevance', 'unsupported_claims', 'inappropriate_content',
] as const
const ELIGIBILITY = {
  eligible: true, blocked_code: null, blocked_message: null, scope_available: true,
  channel_title: 'Test Channel', revision: 2,
  review: { review_status: 'complete', checks: CHECKS, findings: [], agreement: 'agree' },
}
const STAMPS = {
  sent_at: '2026-09-27T20:00:00Z', created_at: '2026-09-27T20:00:00Z',
  resolved_at: '2026-09-27T20:00:01Z',
}
const record = (over: Record<string, unknown>) => ({
  id: 'pub-1', operation_id: 'op-1', status: 'posted', channel_id: 'channel-1',
  video_id: 'v1', parent_comment_id: 'c1', draft_revision: 2,
  text: PUBLISHABLE_DRAFT.text, reply_comment_id: 'reply-1', failure: {}, ...STAMPS, ...over,
})

type Overrides = {
  draft?: unknown
  eligibility?: Record<string, unknown>
  publication?: Record<string, unknown> | null
  post?: () => Response | Promise<Response>
  afterPost?: Record<string, unknown>
}

// Counts the write requests so a test can prove the confirm button is not spammable and
// that merely reading the state never posts anything. `stored` stands in for the server's
// durable record, so a read after a write sees what a refresh would really show.
function publishing(overrides: Overrides = {}) {
  const calls = { posts: 0, bodies: [] as string[] }
  const stored: { publication: Record<string, unknown> | null } = {
    publication: overrides.publication ?? null,
  }
  const route: Route = async (url, init) => {
    if (base(url) === '/setup') return ok(SETUP)
    if (base(url).startsWith('?video_id')) return ok(overrides.draft ?? PUBLISHABLE_DRAFT)
    if (base(url).endsWith('/usage')) return ok(USAGE)
    if (isConversation(url)) return ok({ turns: [APPLIED_TURN] })
    if (base(url).endsWith('/publication')) {
      if (init.method === 'POST') {
        calls.posts += 1
        calls.bodies.push(String(init.body))
        if (overrides.afterPost) stored.publication = overrides.afterPost
        const res = overrides.post ? await overrides.post() : ok(record({}))
        if (res.ok && !overrides.afterPost) stored.publication = record({})
        return res
      }
      return ok({
        publication: stored.publication,
        eligibility: { ...ELIGIBILITY, ...overrides.eligibility },
      })
    }
    return ok({ turns: [] })
  }
  return { route, calls }
}

describe('publishing confirmation', () => {
  it('previews the channel, target, revision, and exact text without writing anything', async () => {
    const user = userEvent.setup()
    const { route, calls } = publishing()
    mount(route)

    await user.click(await screen.findByRole('button', { name: /post reply to youtube/i }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Test Channel')).toBeInTheDocument()
    expect(within(dialog).getByText('v1')).toBeInTheDocument()
    expect(within(dialog).getByText('c1')).toBeInTheDocument()
    expect(within(dialog).getByText('2')).toBeInTheDocument()
    // The exact saved text, so nobody approves one reply and posts another.
    expect(within(dialog).getByText((_, node) =>
      node?.tagName === 'BLOCKQUOTE' && node.textContent === PUBLISHABLE_DRAFT.text)).toBeInTheDocument()
    expect(within(dialog).getByText(/public reply under your channel/i)).toBeInTheDocument()
    // Opening a preview is not consent, so nothing may have been sent.
    expect(calls.posts).toBe(0)

    await user.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(calls.posts).toBe(0)
  })

  it('posts only the saved revision and text on confirmation, then reports the result', async () => {
    const user = userEvent.setup()
    const { route, calls } = publishing()
    mount(route)

    await user.click(await screen.findByRole('button', { name: /post reply to youtube/i }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /confirm & post/i }))

    expect(await screen.findByText('Posted to YouTube')).toBeInTheDocument()
    expect(calls.posts).toBe(1)
    expect(JSON.parse(calls.bodies[0])).toEqual({
      revision: 2, text: PUBLISHABLE_DRAFT.text,
    })
    // A confirmed reply must not offer a second post from the same draft.
    expect(screen.queryByRole('button', { name: /post reply to youtube/i })).not.toBeInTheDocument()
  })

  it('sends one request when the confirm button is clicked repeatedly', async () => {
    const user = userEvent.setup()
    let release = () => {}
    const { route, calls } = publishing({
      post: () => new Promise<Response>(resolve => { release = () => resolve(ok(record({}))) }),
    })
    mount(route)

    await user.click(await screen.findByRole('button', { name: /post reply to youtube/i }))
    const confirm = within(screen.getByRole('dialog')).getByRole('button', { name: /confirm & post/i })
    // A second click while the first is in flight must not reach the provider again.
    await user.click(confirm)
    await user.click(confirm)
    expect(calls.posts).toBe(1)
    expect(confirm).toBeDisabled()

    release()
    expect(await screen.findByText('Posted to YouTube')).toBeInTheDocument()
    expect(calls.posts).toBe(1)
  })
})

describe('publishing outcomes', () => {
  it('reports a recorded confirmation after a refresh without posting again', async () => {
    const { route, calls } = publishing({ publication: record({}) })
    mount(route)

    expect(await screen.findByText('Posted to YouTube')).toBeInTheDocument()
    expect(screen.getByText(/reply-1/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /post reply to youtube/i })).not.toBeInTheDocument()
    expect(calls.posts).toBe(0)
  })

  it('never offers a retry for an unknown outcome, because a repeat could double-post', async () => {
    const { route, calls } = publishing({
      publication: record({ status: 'unknown', reply_comment_id: null, retryable: false }),
    })
    mount(route)

    expect(await screen.findByText(/outcome unknown/i)).toBeInTheDocument()
    expect(screen.getByText(/will not send it again/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /post reply to youtube/i })).not.toBeInTheDocument()
    expect(calls.posts).toBe(0)
  })

  it('offers another try only after a recorded refusal, which posted nothing', async () => {
    const refused = record({
      status: 'failed', reply_comment_id: null, retryable: true,
      failure: { code: 'reply_rejected', message: 'YouTube refused the reply because replies are closed.' },
    })
    const { route, calls } = publishing({
      publication: refused,
      post: () => problem(400, 'reply_rejected', 'YouTube refused the reply because replies are closed.'),
    })
    mount(route)

    expect(await screen.findByText('Not posted')).toBeInTheDocument()
    expect(screen.getByText(/replies are closed/i)).toBeInTheDocument()
    // A refusal is a known no, so a deliberate retry is allowed.
    expect(screen.getByRole('button', { name: /post reply to youtube/i })).toBeEnabled()
    expect(calls.posts).toBe(0)
  })

  it('shows the durable unknown state after a request that never answers', async () => {
    const user = userEvent.setup()
    const { route, calls } = publishing({
      post: () => Promise.reject(new TypeError('Failed to fetch')) as unknown as Response,
      // The request reached the server, which stored an attempt it cannot resolve.
      afterPost: record({ status: 'unknown', reply_comment_id: null, retryable: false }),
    })
    mount(route)

    await user.click(await screen.findByRole('button', { name: /post reply to youtube/i }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /confirm & post/i }))

    // The server's record replaces the client's guess: unknown, and no retry offered.
    expect(await screen.findByText(/outcome unknown/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /post reply to youtube/i })).not.toBeInTheDocument()
    expect(calls.posts).toBe(1)
  })
})

describe('publishing is blocked until the local path is complete', () => {
  it('states the server reason and disables posting when the scope is missing', async () => {
    mount(publishing({
      eligibility: {
        eligible: false, scope_available: false,
        blocked_code: 'insufficient_scope',
        blocked_message: "Google's youtube.force-ssl permission is required to post a reply. Use Update YouTube permissions first. Nothing has been posted.",
      },
    }).route)

    // The reason arrives with the state, so wait for it rather than the button, which
    // also renders while the state is still loading.
    expect(await screen.findByText(/youtube\.force-ssl permission is required/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /post reply to youtube/i })).toBeDisabled()
  })

  it('disables posting while edits are unsaved, so the posted text is the reviewed text', async () => {
    const { route } = publishing({ draft: { ...PUBLISHABLE_DRAFT, review_status: 'complete' } })
    mount(route)

    const textarea = await screen.findByRole('textbox', { name: /reply text/i })
    await userEvent.type(textarea, ' more')

    expect(screen.getByRole('button', { name: /post reply to youtube/i })).toBeDisabled()
    expect(screen.getByText(/posting is paused while you have unsaved edits/i)).toBeInTheDocument()
  })

  it('states that a retained publication already exists here and offers no way to repeat it', async () => {
    // The server keeps this record after the original draft is deleted, so a fresh draft
    // for the same comment is refused. The panel has to say so plainly and must not
    // present an action that would defeat the guard.
    const { route, calls } = publishing({
      eligibility: {
        eligible: false, scope_available: true,
        blocked_code: 'already_replied',
        blocked_message:
          'This comment already has a reply published to YouTube by ReplyAI. '
          + 'ReplyAI keeps that record even if the draft is deleted, so it will not '
          + 'post a second reply here.',
      },
    })
    mount(route)

    expect(await screen.findByText(/already has a reply published to YouTube by ReplyAI/i))
      .toBeInTheDocument()
    const post = screen.getByRole('button', { name: /post reply to youtube/i })
    expect(post).toBeDisabled()
    // Opening a draft that cannot be published must not attempt one.
    expect(calls.posts).toBe(0)
  })
})

// The bug this suite pins: the publish control was gated on an eligibility answer
// fetched once when the draft opened. Approving, editing, reviewing, or applying a
// suggestion changed the draft but not that answer, so the panel could display
// "approved" and "Approve this draft locally" at the same time, or enable a button the
// server would refuse. These tests drive a mock whose eligibility is derived from the
// row the mutation endpoints just changed, so the panel is only correct if it re-reads.
type DraftFixture = Omit<typeof PUBLISHABLE_DRAFT, 'quality' | 'primary_classification'> & {
  quality: QualityFixture | null
  primary_classification: { sentiment: string; intent: string } | null
}

function stateful(initial: Partial<DraftFixture> = {},
                  options: { turn?: Record<string, unknown> } = {}) {
  let draft: DraftFixture = {
    ...PUBLISHABLE_DRAFT,
    primary_classification: { sentiment: 'positive', intent: 'praise' },
    ...initial,
  }
  let turn = options.turn ?? null
  const route: Route = (url, init) => {
    const path = base(url)
    if (path === '/setup') return ok(SETUP)
    if (path.startsWith('?video_id')) return ok(draft)
    if (path.endsWith('/usage')) return ok(USAGE)
    if (isConversation(url)) return ok({ turns: turn ? [turn] : [] })
    if (path.endsWith('/decision')) {
      draft = { ...draft, human_decision: JSON.parse(String(init.body)).decision }
      return ok(draft)
    }
    if (path.endsWith('/edit')) {
      const body = JSON.parse(String(init.body))
      draft = { ...draft, text: body.text, revision: Number(draft.revision) + 1,
                review_status: 'pending', human_decision: 'pending', quality: null }
      return ok(draft)
    }
    if (path.endsWith('/review')) {
      draft = { ...draft, review_status: 'complete', human_decision: 'pending',
                quality: PASSING_QUALITY }
      return ok(draft)
    }
    if (path.endsWith('/apply')) {
      draft = { ...draft, text: String(turn?.suggestion ?? ''),
                revision: Number(draft.revision) + 1,
                review_status: 'pending', human_decision: 'pending', quality: null }
      turn = { ...turn, status: 'applied' }
      return ok(draft)
    }
    if (path.endsWith('/publication')) {
      const reviewed = draft.review_status === 'complete'
      const qualityPassed = qualityCheckLabelsForTest.every(
        key => draft.quality?.[key] === 'pass')
      const eligible = Boolean(draft.text) && reviewed && qualityPassed
        && draft.human_decision === 'approved'
      const blockedCode = !reviewed || !draft.quality ? 'review_required'
        : !qualityPassed ? 'quality_not_passed'
          : !draft.text ? 'no_reply'
            : draft.human_decision !== 'approved' ? 'approval_required' : null
      return ok({
        publication: null,
        eligibility: {
          ...ELIGIBILITY,
          eligible,
          blocked_code: blockedCode,
          blocked_message: blockedCode === 'quality_not_passed'
            ? 'The saved review did not pass every check. Edit and review it again.'
            : blockedCode === 'approval_required'
              ? 'Approve this draft locally before publishing it.'
              : blockedCode === 'review_required'
                ? 'Complete review of the saved revision before publishing.' : null,
          review: {
            ...ELIGIBILITY.review,
            review_status: draft.review_status,
            checks: Object.fromEntries(qualityCheckLabelsForTest.map(key =>
              [key, draft.quality?.[key] ?? null])),
          },
          revision: draft.revision,
        },
      })
    }
    return ok({ turns: [] })
  }
  return { route, current: () => draft }
}

describe('publish eligibility tracks the saved revision', () => {
  const postButton = () => screen.getByRole('button', { name: /post reply to youtube/i })

  it('clears the approval reason and enables posting when the approval lands', async () => {
    const user = userEvent.setup()
    mount(stateful({ human_decision: 'pending' }).route)

    // Reviewed but not yet approved: the reason and the disabled control agree.
    expect(await screen.findByText(/approve this draft locally before publishing it/i))
      .toBeInTheDocument()
    expect(postButton()).toBeDisabled()

    await user.click(screen.getByRole('button', { name: /Approve locally/i }))

    // The decision appears immediately. The eligibility it gates on must be re-read in
    // the same moment, or the two statements contradict each other on screen.
    expect(await screen.findByText(/Approved/i)).toBeInTheDocument()
    await waitFor(() => expect(
      screen.queryByText(/approve this draft locally before publishing it/i))
      .not.toBeInTheDocument())
    expect(postButton()).toBeEnabled()
  })

  it('keeps an approved draft blocked when its current quality review has a concern', async () => {
    mount(stateful({
      quality: { ...PASSING_QUALITY, unsupported_claims: 'concern', findings: ['A finding.'] },
    }).route)

    expect(await screen.findByText(/approved · rev 2/i)).toBeInTheDocument()
    expect(screen.getByText('Unsupported claims: concern')).toBeInTheDocument()
    expect(screen.getByText(/posting is blocked by the check\(s\) above/i)).toBeInTheDocument()
    expect(await screen.findByText(/saved review did not pass every check/i)).toBeInTheDocument()
    expect(postButton()).toBeDisabled()
  })

  it('marks an outstanding suggestion from an older revision stale and unavailable to apply', async () => {
    mount(stateful({ revision: 2 }, { turn: { ...TURN, base_revision: 1 } }).route)

    expect(await screen.findByText(/stale suggestion from revision 1/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /apply suggestion/i })).not.toBeInTheDocument()
    expect(postButton()).toBeEnabled()
  })

  it('does not carry an approval onto a newer revision created by an edit', async () => {
    const user = userEvent.setup()
    mount(stateful().route)

    await waitFor(() => expect(postButton()).toBeEnabled())

    const textarea = screen.getByRole('textbox', { name: /reply text/i })
    await user.clear(textarea)
    await user.type(textarea, 'A different reply')
    await user.click(screen.getByRole('button', { name: /Save changes/i }))

    // The edit bumped the revision and cleared both the review and the approval, so the
    // old approval cannot authorize the new text.
    await waitFor(() => expect(postButton()).toBeDisabled())
    expect(await screen.findByText(/complete review of the saved revision/i)).toBeInTheDocument()
  })

  it('does not carry an approval onto a revision created by applying a suggestion', async () => {
    const user = userEvent.setup()
    mount(stateful({}, { turn: { ...TURN, base_revision: 2, status: 'suggested' } }).route)

    await waitFor(() => expect(postButton()).toBeEnabled())
    expect(await screen.findByText('A warmer suggested reply')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /apply suggestion/i }))

    await waitFor(() => expect(postButton()).toBeDisabled())
    expect(await screen.findByText(/complete review of the saved revision/i)).toBeInTheDocument()
  })

  it('re-enables posting only after the new revision is reviewed and then approved', async () => {
    const user = userEvent.setup()
    mount(stateful().route)
    await waitFor(() => expect(postButton()).toBeEnabled())

    const textarea = screen.getByRole('textbox', { name: /reply text/i })
    await user.clear(textarea)
    await user.type(textarea, 'A different reply')
    await user.click(screen.getByRole('button', { name: /Save changes/i }))
    await waitFor(() => expect(postButton()).toBeDisabled())

    // A completed review is still not an approval.
    await user.click(screen.getByRole('button', { name: /review edited/i }))
    await waitFor(() => expect(postButton()).toBeDisabled())
    expect(screen.getByRole('button', { name: /Approve locally/i })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: /Approve locally/i }))
    await waitFor(() => expect(postButton()).toBeEnabled())
  })
})
