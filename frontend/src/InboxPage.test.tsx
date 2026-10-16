import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import InboxPage from './pages/InboxPage'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('InboxPage', () => {
  it('renders comments and badges based on reply_status', () => {
    const comments = [
      { comment_id: '1', author_name: 'Alice', text: 'Hello', reply_status: 'Posted', published_at: '2026-10-01' },
      { comment_id: '2', author_name: 'Bob', text: 'Needs review', reply_status: 'Needs review' },
      { comment_id: '3', author_name: 'Charlie', text: 'Draft ready', reply_status: 'Draft ready' }
    ]
    
    const appData = {
      comments,
      commentsLoading: false,
      selectedComment: null,
      setSelectedComment: vi.fn(),
      session: { connected: true },
      selectVideo: vi.fn()
    }
    
    render(<InboxPage appData={appData} />)
    
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.getByText('Bob')).toBeInTheDocument()
    
    expect(screen.getAllByText(/Needs review/).length).toBeGreaterThan(0)
    
    // Switch to review tab
    fireEvent.click(screen.getAllByRole('button', { name: /Needs review/i })[0])
    expect(screen.queryByText('Alice')).not.toBeInTheDocument()
    expect(screen.getByText('Bob')).toBeInTheDocument()
  })
})

describe('Comment selection and workspace context', () => {
  it('loads each selected comment and clears the previous draft during rapid switches', async () => {
    const comments = [
      { comment_id: 'c1', author_name: 'Alice', text: 'First comment', reply_status: 'No draft', video_id: 'vid1', video_title: null },
      { comment_id: 'c2', author_name: 'Bob', text: 'Second comment', reply_status: 'No draft', video_id: 'vid1', video_title: null },
      { comment_id: 'c3', author_name: 'Charlie', text: 'Third comment', reply_status: 'No draft', video_id: 'vid2', video_title: null },
    ]
    const videos = [
      { video_id: 'vid1', title: 'Video One', thumbnail_url: null, published_at: null },
      { video_id: 'vid2', title: 'Video Two', thumbnail_url: null, published_at: null },
    ]
    const lookups: Array<{ videoId: string | null; commentId: string | null }> = []
    let resolveSecondDraft: ((response: Response) => void) | undefined
    const savedDraft = (commentId: string, videoId: string) => ({
      id: `draft-${commentId}`, text: `Reply for ${commentId}`, revision: 1,
      original_comment: `Original ${commentId}`, video_id: videoId, comment_id: commentId,
      primary_classification: null, secondary_classification: null, agreement: 'unknown',
      generation_status: 'complete', review_status: 'complete', human_decision: 'pending',
      quality: null, failure: {}, providers: {}, token_usage: {},
    })
    const json = (body: unknown) => new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })

    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/drafts/setup') {
        return Promise.resolve(json({ configured: true, missing: [], invalid: [] }))
      }
      const videoId = url.searchParams.get('video_id')
      const commentId = url.searchParams.get('comment_id')
      if (videoId && commentId) {
        lookups.push({ videoId, commentId })
        if (commentId === 'c2') {
          return new Promise<Response>((resolve) => { resolveSecondDraft = resolve })
        }
        return Promise.resolve(json(savedDraft(commentId, videoId)))
      }
      if (url.pathname.endsWith('/usage')) {
        return Promise.resolve(json({
          entries: [],
          totals: { requests: 0, reported_requests: 0, unknown_requests: 0, discarded_requests: 0, input_tokens: 0, output_tokens: 0 },
          coverage: { complete: true, pre_ledger_activity: false, unbacked_snapshot_stages: [], notice: null, unknown_notice: null },
        }))
      }
      if (url.pathname.endsWith('/conversation')) return Promise.resolve(json({ turns: [] }))
      if (url.pathname.endsWith('/publication')) return Promise.resolve(json({ publication: null, eligibility: { eligible: false } }))
      return Promise.resolve(json(null))
    })

    function InboxHarness() {
      const [selectedComment, setSelectedComment] = useState<string | null>('c1')
      const [selectedVideo, selectVideo] = useState<typeof videos[number] | null>(null)
      return <InboxPage appData={{
        comments, videos, commentsLoading: false, selectedComment, setSelectedComment,
        selectedVideo, selectVideo, session: { connected: true, csrf_token: 'csrf' },
        loadComments: vi.fn(),
      }} />
    }

    render(<InboxHarness />)
    expect(await screen.findByLabelText('reply text')).toHaveValue('Reply for c1')
    expect(lookups).toContainEqual({ videoId: 'vid1', commentId: 'c1' })
    const workspaceGutter = screen.getByLabelText('Local reply draft').parentElement
    expect(workspaceGutter).toHaveClass('p-4', 'md:p-6', 'overflow-y-auto', 'min-w-0')
    expect(screen.getByLabelText('reply text')).toHaveClass('box-border', 'min-w-0', 'max-w-full')

    fireEvent.click(screen.getByRole('button', { name: 'Select comment by Bob' }))
    await waitFor(() => expect(lookups).toContainEqual({ videoId: 'vid1', commentId: 'c2' }))
    expect(screen.getByRole('button', { name: 'Select comment by Bob' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByDisplayValue('Reply for c1')).not.toBeInTheDocument()
    expect(screen.getByText('Video One')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Select comment by Charlie' }))
    await waitFor(() => expect(lookups).toContainEqual({ videoId: 'vid2', commentId: 'c3' }))
    expect(await screen.findByLabelText('reply text')).toHaveValue('Reply for c3')
    expect(screen.getByText('Video Two')).toBeInTheDocument()

    await act(async () => {
      resolveSecondDraft?.(json(savedDraft('c2', 'vid1')))
      await Promise.resolve()
    })
    expect(screen.getByLabelText('reply text')).toHaveValue('Reply for c3')
  })
})

describe('Inbox loading and error states', () => {
  it('shows the load error instead of an empty state', () => {
    const appData = {
      comments: [], commentsLoading: false, commentsError: 'Comments could not be loaded.',
      selectedComment: null, setSelectedComment: vi.fn(), selectedVideo: null,
      session: { connected: true }, selectVideo: vi.fn(), loadComments: vi.fn(),
    }
    render(<InboxPage appData={appData} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Comments could not be loaded.')
    expect(screen.queryByText(/No comments available/)).not.toBeInTheDocument()
  })

  it('reloads comments from the Sync comments button', () => {
    const loadComments = vi.fn()
    const appData = {
      comments: [], commentsLoading: false, commentsError: null,
      selectedComment: null, setSelectedComment: vi.fn(),
      selectedVideo: { video_id: 'vid1', title: 'Video One' },
      session: { connected: true }, selectVideo: vi.fn(), loadComments,
    }
    render(<InboxPage appData={appData} />)
    fireEvent.click(screen.getByRole('button', { name: /sync comments/i }))
    expect(loadComments).toHaveBeenCalledWith('vid1', null, false)
  })
})
