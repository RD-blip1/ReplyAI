import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import JobsPage from './pages/JobsPage'
import * as api from './lib/api'
import type { JobList, JobStats, ReplyJob } from './lib/types'
import type { useAppData } from './hooks/useAppData'

vi.mock('./lib/api')

function job(overrides: Partial<ReplyJob> = {}): ReplyJob {
  return {
    id: 'job-1',
    batch_id: 'batch-1',
    channel_id: 'channel-1',
    video_id: 'abcdefghijk',
    comment_id: 'comment-1',
    draft_id: null,
    status: 'published',
    attempts: 1,
    max_attempts: 3,
    recoveries: 0,
    last_error: {},
    outcome: { decision: 'published' },
    next_run_at: null,
    lease_expires_at: null,
    created_at: '2026-10-18T00:00:00+00:00',
    finished_at: '2026-10-18T00:00:01+00:00',
    ...overrides,
  }
}

const emptyStats: JobStats = { counts: {}, due_now: 0, total: 0 }

function connectedAppData() {
  return {
    session: { csrf_token: 'csrf-token', connected: true, channel: { channel_id: 'channel-1' } },
    sessionLoading: false,
    sessionError: null,
  } as unknown as ReturnType<typeof useAppData>
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('JobsPage', () => {
  it('shows a summary and lists jobs', async () => {
    const list: JobList = {
      items: [job(), job({ id: 'job-2', comment_id: 'comment-2', status: 'failed', last_error: { code: 'transient_error' }, outcome: {} })],
      total: 2,
    }
    const stats: JobStats = { counts: { published: 1, failed: 1 }, due_now: 0, total: 2 }
    vi.mocked(api.apiRequest).mockResolvedValueOnce(list).mockResolvedValueOnce(stats)

    render(<JobsPage appData={connectedAppData()} />)

    expect(await screen.findByText(/comment-1/)).toBeInTheDocument()
    expect(screen.getByText(/comment-2/)).toBeInTheDocument()
    // Published and failed each render in the table plus the summary cards.
    expect(screen.getAllByText('Published').length).toBeGreaterThan(0)
    expect(screen.getByText('transient_error')).toBeInTheDocument()
  })

  it('cancels a queued job with the CSRF token', async () => {
    const list: JobList = { items: [job({ id: 'job-3', status: 'pending', outcome: {} })], total: 1 }
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(list).mockResolvedValueOnce({ ...emptyStats, counts: { pending: 1 }, total: 1 })
      .mockResolvedValueOnce(list.items[0]) // cancel response
      .mockResolvedValueOnce(list).mockResolvedValueOnce({ ...emptyStats, counts: { pending: 1 }, total: 1 })

    render(<JobsPage appData={connectedAppData()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(api.apiRequest).toHaveBeenCalledWith(
      '/api/batch/jobs/job-3/cancel',
      expect.objectContaining({ method: 'POST', headers: { 'X-CSRF-Token': 'csrf-token' } }),
    ))
  })

  it('requeues a terminal job', async () => {
    const failed = job({ id: 'job-4', status: 'review', outcome: { decision: 'requires_human' } })
    const list: JobList = { items: [failed], total: 1 }
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(list).mockResolvedValueOnce({ ...emptyStats, counts: { review: 1 }, total: 1 })
      .mockResolvedValueOnce(failed)
      .mockResolvedValueOnce(list).mockResolvedValueOnce({ ...emptyStats, counts: { review: 1 }, total: 1 })

    render(<JobsPage appData={connectedAppData()} />)
    fireEvent.click(await screen.findByRole('button', { name: /Requeue job for comment-1/ }))

    await waitFor(() => expect(api.apiRequest).toHaveBeenCalledWith(
      '/api/batch/jobs/job-4/requeue',
      expect.objectContaining({ method: 'POST', headers: { 'X-CSRF-Token': 'csrf-token' } }),
    ))
  })

  it('enqueues jobs from the form and reports duplicates', async () => {
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce({ items: [], total: 0 }).mockResolvedValueOnce(emptyStats) // initial load
      .mockResolvedValueOnce({ created: ['a', 'b'], duplicates: 1, batch_id: 'batch' }) // enqueue
      .mockResolvedValueOnce({ items: [], total: 0 }).mockResolvedValueOnce(emptyStats) // reload

    render(<JobsPage appData={connectedAppData()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Enqueue jobs' }))
    fireEvent.change(screen.getByLabelText('Video id'), { target: { value: 'abcdefghijk' } })
    fireEvent.change(screen.getByLabelText('Comment ids'), { target: { value: 'a, b\nc' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enqueue' }))

    await waitFor(() => expect(api.apiRequest).toHaveBeenCalledWith(
      '/api/batch/jobs/enqueue',
      expect.objectContaining({
        method: 'POST',
        headers: { 'X-CSRF-Token': 'csrf-token' },
        body: JSON.stringify({ video_id: 'abcdefghijk', comment_ids: ['a', 'b', 'c'] }),
      }),
    ))
    expect(await screen.findByText('Enqueued 2 job(s); 1 already queued.')).toBeInTheDocument()
  })

  it('prompts to connect when no channel is connected', async () => {
    render(<JobsPage appData={{
      session: { csrf_token: '', connected: false, channel: null },
      sessionLoading: false,
      sessionError: null,
    } as unknown as ReturnType<typeof useAppData>} />)

    expect(api.apiRequest).not.toHaveBeenCalled()
    expect(screen.getByText('Connect a YouTube channel to view reply jobs.')).toBeInTheDocument()
  })
})
