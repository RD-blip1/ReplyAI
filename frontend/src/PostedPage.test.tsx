import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import PostedPage from './pages/PostedPage'
import * as api from './lib/api'
import type { PublicationPage } from './lib/types'
import type { useAppData } from './hooks/useAppData'

vi.mock('./lib/api')

const connectedAppData = (channelId = 'channel-1') => ({
  session: { connected: true, channel: { channel_id: channelId } },
  sessionLoading: false,
  sessionError: null,
}) as ReturnType<typeof useAppData>

const appDataWithMetadata = (overrides: Record<string, unknown> = {}) => ({
  ...connectedAppData(),
  videos: [{
    video_id: 'video-1',
    title: 'How I built this',
    thumbnail_url: null,
    published_at: null,
    watch_url: 'https://youtube.com/watch?v=video-1',
  }],
  comments: [{
    comment_id: 'comment-1',
    author_name: 'Ananya',
    author_thumbnail_url: null,
    published_at: null,
    text: 'Can you share the repo?',
    total_reply_count: 0,
    reply_status: 'Posted',
    video_id: 'video-1',
    video_title: 'How I built this',
  }],
  ...overrides,
}) as unknown as ReturnType<typeof useAppData>

const publication = (overrides: Partial<PublicationPage['items'][number]> = {}): PublicationPage['items'][number] => ({
  id: 'pub-1',
  channel_id: 'channel-1',
  video_id: 'video-1',
  parent_comment_id: 'comment-1',
  reply_comment_id: null,
  text: 'Reply text',
  status: 'posted',
  created_at: '2026-10-08T12:00:00Z',
  resolved_at: null,
  failure: {},
  draft_revision: 1,
  comment_text: null,
  ...overrides,
})

const page = (...items: PublicationPage['items']): PublicationPage => ({ total: items.length, items })
const resolveApi = (value: PublicationPage) =>
  vi.mocked(api.apiRequest).mockResolvedValueOnce(value)

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('PostedPage', () => {
  it('retrieves publication history from the API route and renders all canonical statuses', async () => {
    resolveApi(page(
      publication({ status: 'posted', text: 'Thanks for watching!', reply_comment_id: 'reply-1' }),
      publication({ id: 'pub-2', status: 'failed', text: 'A rejected reply', failure: { message: 'YouTube refused it' } }),
      publication({ id: 'pub-3', status: 'unknown', text: 'Outcome needs checking' }),
      publication({ id: 'pub-4', status: 'sending', text: 'Abandoned send' }),
    ))

    render(<PostedPage appData={connectedAppData()} />)

    expect(await screen.findByText('Thanks for watching!')).toBeInTheDocument()
    expect(screen.getByText('A rejected reply')).toBeInTheDocument()
    expect(screen.getByText('Outcome needs checking')).toBeInTheDocument()
    expect(screen.getByText('Abandoned send')).toBeInTheDocument()
    expect(screen.getByText('View on YouTube')).toHaveAttribute('href', 'https://youtube.com/comments?lc=reply-1')
    const unconfirmedCard = screen.getAllByText('Unconfirmed')[0].parentElement
    expect(unconfirmedCard && within(unconfirmedCard).getByText('2')).toBeInTheDocument()
    expect(api.apiRequest).toHaveBeenCalledWith(
      '/api/youtube/publications?limit=100&offset=0',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('represents a successful empty history separately from filtered-out records', async () => {
    resolveApi({ items: [], total: 0 })
    render(<PostedPage appData={connectedAppData()} />)

    expect(await screen.findByText('No publication history yet.')).toBeInTheDocument()
    expect(screen.queryByText('No publication history matches your filters.')).not.toBeInTheDocument()
    expect(screen.getAllByText('0')).toHaveLength(3)
  })

  it('loads all pages before calculating counters and applying search/status filters', async () => {
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce({
        items: [publication({ id: 'pub-1', status: 'posted', text: 'Warm reply' })],
        total: 2,
      })
      .mockResolvedValueOnce({
        items: [publication({ id: 'pub-2', status: 'failed', text: 'Different message' })],
        total: 2,
      })
    render(<PostedPage appData={connectedAppData()} />)

    expect(await screen.findByText('Different message')).toBeInTheDocument()
    expect(api.apiRequest).toHaveBeenNthCalledWith(
      2,
      '/api/youtube/publications?limit=100&offset=1',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )

    fireEvent.change(screen.getByPlaceholderText('Search replies...'), { target: { value: ' warm ' } })
    expect(screen.getByText('Warm reply')).toBeInTheDocument()
    expect(screen.queryByText('Different message')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Failed' }))
    expect(screen.getByText('No publication history matches your filters.')).toBeInTheDocument()
  })

  it('shows a backend failure and retries history successfully', async () => {
    vi.mocked(api.apiRequest)
      .mockRejectedValueOnce(new Error('Publication history is temporarily unavailable.'))
      .mockResolvedValueOnce(page(publication({ text: 'Loaded after retry' })))
    render(<PostedPage appData={connectedAppData()} />)

    expect(await screen.findByText('Publication history is temporarily unavailable.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading history' }))
    expect(await screen.findByText('Loaded after retry')).toBeInTheDocument()
    expect(api.apiRequest).toHaveBeenCalledTimes(2)
  })

  it('refreshes durable publication status using the read-only history endpoint', async () => {
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(page(publication({ text: 'Before refresh' })))
      .mockResolvedValueOnce(page(publication({ text: 'After refresh' })))
    render(<PostedPage appData={connectedAppData()} />)

    expect(await screen.findByText('Before refresh')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    expect(await screen.findByText('After refresh')).toBeInTheDocument()
    expect(api.apiRequest).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.apiRequest).mock.calls.every(([, options]) => options?.method !== 'POST')).toBe(true)
  })

  it('does not fetch while disconnected and gives an explicit connection state', () => {
    render(<PostedPage appData={{
      ...connectedAppData(),
      session: { connected: false, channel: null },
    } as ReturnType<typeof useAppData>} />)

    expect(screen.getByText('Connect a YouTube channel to view publication history.')).toBeInTheDocument()
    expect(screen.getAllByText('--')).toHaveLength(3)
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeDisabled()
    expect(api.apiRequest).not.toHaveBeenCalled()
  })

  it('aborts and ignores an older response after the connected channel changes', async () => {
    let resolveOld: ((value: PublicationPage) => void) | undefined
    vi.mocked(api.apiRequest)
      .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
      .mockResolvedValueOnce(page(publication({ id: 'new-pub', text: 'Current channel reply' })))

    const { rerender } = render(<PostedPage appData={connectedAppData('channel-1')} />)
    await waitFor(() => expect(api.apiRequest).toHaveBeenCalledTimes(1))
    const firstSignal = vi.mocked(api.apiRequest).mock.calls[0][1]?.signal

    rerender(<PostedPage appData={connectedAppData('channel-2')} />)
    expect(await screen.findByText('Current channel reply')).toBeInTheDocument()
    expect(firstSignal?.aborted).toBe(true)

    await act(async () => {
      resolveOld?.(page(publication({ id: 'old-pub', text: 'Stale channel reply' })))
    })
    expect(screen.queryByText('Stale channel reply')).not.toBeInTheDocument()
    expect(screen.getByText('Current channel reply')).toBeInTheDocument()
  })

  it('enriches history with the video title, comment text, and author it already holds', async () => {
    resolveApi(page(publication({
      status: 'posted',
      text: 'Thanks for watching!',
      comment_text: 'Can you share the repo?',
    })))

    render(<PostedPage appData={appDataWithMetadata()} />)

    expect(await screen.findByText('How I built this')).toBeInTheDocument()
    expect(screen.getByText('Can you share the repo?')).toBeInTheDocument()
    expect(screen.getByText('Comment by Ananya')).toBeInTheDocument()
    // The raw identifiers stay available, but as secondary context rather than the headline.
    expect(screen.getByText(/video video-1 · comment comment-1/)).toBeInTheDocument()
  })

  it('falls back to stored comment text, then to an explicit placeholder, when metadata is missing', async () => {
    resolveApi(page(
      publication({ id: 'pub-1', video_id: 'video-1', parent_comment_id: 'comment-1', comment_text: 'Stored snapshot only' }),
      publication({ id: 'pub-2', video_id: 'video-2', parent_comment_id: 'comment-2', comment_text: null }),
    ))

    render(<PostedPage appData={appDataWithMetadata()} />)

    // No appData lookup, but the backend snapshot still renders.
    expect(await screen.findByText('Stored snapshot only')).toBeInTheDocument()
    // Neither source knows this comment, so the row says so instead of showing a blank.
    expect(screen.getByText('Comment text unavailable')).toBeInTheDocument()
    // Unknown video falls back to its raw id rather than a blank title.
    expect(screen.getByText('Video: video-2')).toBeInTheDocument()
  })
})
