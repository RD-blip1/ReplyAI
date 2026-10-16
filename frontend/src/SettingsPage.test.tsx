import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SettingsPage from './pages/SettingsPage'
import * as api from './lib/api'
import type { ReplyPreferences } from './lib/types'
import type { useAppData } from './hooks/useAppData'

vi.mock('./lib/api')

const savedPreferences: ReplyPreferences = {
  tone: 'Professional',
  reply_length: 'Detailed',
  language: 'Hindi',
  channel_instructions: 'Use concise sentences.',
  revision: 2,
}
const defaultMode = { mode: 'manual_review' as const, revision: 0 }

function appData(channelId: string | null = 'channel-1', commentsScopeAvailable = true) {
  return {
    session: {
      csrf_token: 'csrf-token',
      connected: Boolean(channelId),
      channel: channelId ? {
        channel_id: channelId,
        channel_title: 'Test channel',
        comments_scope_available: commentsScopeAvailable,
      } : null,
    },
    sessionLoading: false,
    connectYouTube: vi.fn(),
    disconnectYouTube: vi.fn(),
  } as unknown as ReturnType<typeof useAppData>
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('SettingsPage reply preferences', () => {
  it('loads saved values and persists edited preferences', async () => {
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(savedPreferences)
      .mockResolvedValueOnce(defaultMode)
      .mockResolvedValueOnce({ ...savedPreferences, tone: 'Casual', revision: 3 })
    const onDirtyChange = vi.fn()
    const view = render(<SettingsPage appData={appData()} onDirtyChange={onDirtyChange} />)

    expect(screen.getByText('Loading saved preferences…')).toBeInTheDocument()
    expect(await screen.findByRole('combobox', { name: 'Tone' })).toHaveValue('Professional')
    expect(screen.getByRole('combobox', { name: 'Reply length' })).toHaveValue('Detailed')
    expect(screen.getByRole('combobox', { name: 'Language' })).toHaveValue('Hindi')
    expect(screen.getByLabelText('Channel instructions')).toHaveValue('Use concise sentences.')
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled()

    fireEvent.change(screen.getByRole('combobox', { name: 'Tone' }), { target: { value: 'Casual' } })
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
    fireEvent.click(screen.getByRole('button', { name: 'Save preferences' }))

    await waitFor(() => expect(api.apiRequest).toHaveBeenCalledWith('/api/preferences', expect.objectContaining({
      method: 'PUT',
      headers: { 'X-CSRF-Token': 'csrf-token' },
      body: JSON.stringify({
        tone: 'Casual',
        reply_length: 'Detailed',
        language: 'Hindi',
        channel_instructions: 'Use concise sentences.',
        revision: 2,
      }),
    })))
    expect(await screen.findByText('Preferences saved')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled()
    expect(onDirtyChange).toHaveBeenLastCalledWith(false)

    fireEvent.change(screen.getByRole('combobox', { name: 'Reply length' }), { target: { value: 'Short' } })
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.getByRole('combobox', { name: 'Reply length' })).toHaveValue('Detailed')
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()

    view.unmount()
    vi.mocked(api.apiRequest).mockResolvedValueOnce({ ...savedPreferences, tone: 'Casual', revision: 3 })
    vi.mocked(api.apiRequest).mockResolvedValueOnce(defaultMode)
    render(<SettingsPage appData={appData()} onDirtyChange={vi.fn()} />)
    expect(await screen.findByRole('combobox', { name: 'Tone' })).toHaveValue('Casual')
  })

  it('reports load errors and retries the request', async () => {
    vi.mocked(api.messageOf).mockImplementation(
      (cause, fallback) => cause instanceof Error ? cause.message : fallback,
    )
    vi.mocked(api.apiRequest)
      .mockRejectedValueOnce(new Error('Server unavailable.'))
      .mockResolvedValueOnce(defaultMode)
      .mockResolvedValueOnce(savedPreferences)
    render(<SettingsPage appData={appData()} onDirtyChange={vi.fn()} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Server unavailable.')
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading' }))
    expect(await screen.findByRole('combobox', { name: 'Tone' })).toHaveValue('Professional')
    expect(vi.mocked(api.apiRequest).mock.calls.filter(([path]) => path === '/api/preferences')).toHaveLength(2)
  })

  it('keeps edits after a save failure and retries without overlapping saves', async () => {
    vi.mocked(api.messageOf).mockImplementation(
      (cause, fallback) => cause instanceof Error ? cause.message : fallback,
    )
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(savedPreferences)
      .mockResolvedValueOnce(defaultMode)
      .mockRejectedValueOnce(new Error('Save failed.'))
      .mockResolvedValueOnce({ ...savedPreferences, tone: 'Casual', revision: 3 })
    render(<SettingsPage appData={appData()} onDirtyChange={vi.fn()} />)
    expect(await screen.findByRole('combobox', { name: 'Tone' })).toHaveValue('Professional')

    fireEvent.change(screen.getByRole('combobox', { name: 'Tone' }), { target: { value: 'Casual' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save preferences' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save preferences' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Save failed.')
    expect(screen.getByRole('combobox', { name: 'Tone' })).toHaveValue('Casual')
    expect(vi.mocked(api.apiRequest).mock.calls.filter(([, options]) => options?.method === 'PUT')).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
    expect(await screen.findByText('Preferences saved')).toBeInTheDocument()
    expect(vi.mocked(api.apiRequest).mock.calls.filter(([, options]) => options?.method === 'PUT')).toHaveLength(2)
  })

  it('disables preference controls when disconnected', async () => {
    render(<SettingsPage appData={appData(null)} onDirtyChange={vi.fn()} />)

    expect(screen.getByText('Connect a YouTube channel to manage its reply preferences.')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Tone' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled()
    expect(api.apiRequest).not.toHaveBeenCalled()
  })

  it('offers the permission upgrade without disconnecting the existing channel', async () => {
    const data = appData('channel-1', false)
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(savedPreferences)
      .mockResolvedValueOnce(defaultMode)
    render(<SettingsPage appData={data} onDirtyChange={vi.fn()} />)

    const updatePermissions = await screen.findByRole('button', {
      name: 'Update YouTube permissions',
    })
    expect(screen.getByText(/Comment access and reply posting require youtube\.force-ssl/))
      .toBeInTheDocument()
    fireEvent.click(updatePermissions)

    expect(data.connectYouTube).toHaveBeenCalledWith(true)
    expect(data.disconnectYouTube).not.toHaveBeenCalled()
  })

  it('ignores a stale settings response after switching channels', async () => {
    let resolveFirst!: (value: ReplyPreferences) => void
    vi.mocked(api.apiRequest)
      .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
      .mockResolvedValueOnce(defaultMode)
      .mockImplementationOnce(path => path === '/api/publishing-settings'
        ? Promise.resolve(defaultMode)
        : Promise.resolve({ ...savedPreferences, tone: 'Casual' }))
      .mockResolvedValueOnce(defaultMode)
    const { rerender } = render(<SettingsPage key="channel-1" appData={appData('channel-1')} onDirtyChange={vi.fn()} />)
    rerender(<SettingsPage key="channel-2" appData={appData('channel-2')} onDirtyChange={vi.fn()} />)

    expect(await screen.findByRole('combobox', { name: 'Tone' })).toHaveValue('Casual')
    resolveFirst({ ...savedPreferences, tone: 'Friendly' })
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Tone' })).toHaveValue('Casual'))
  })

  it('requires confirmation, persists automatic modes, and switches back to Manual review', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(savedPreferences)
      .mockResolvedValueOnce(defaultMode)
      .mockResolvedValueOnce({ mode: 'auto_on_agreement', revision: 1 })
      .mockResolvedValueOnce({ mode: 'manual_review', revision: 2 })
    render(<SettingsPage appData={appData()} onDirtyChange={vi.fn()} />)

    const automatic = await screen.findByRole('radio', { name: /auto-publish on agreement/i })
    fireEvent.click(automatic)

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('at least 80% confidence'))
    await waitFor(() => expect(automatic).toHaveAttribute('aria-checked', 'true'))
    expect(screen.getByText(/Active mode:/)).toHaveTextContent('Auto-publish on agreement')
    expect(await screen.findByText('Publishing mode saved.')).toBeInTheDocument()
    expect(api.apiRequest).toHaveBeenCalledWith('/api/publishing-settings', expect.objectContaining({
      method: 'PUT',
      headers: { 'X-CSRF-Token': 'csrf-token' },
      body: JSON.stringify({
        mode: 'auto_on_agreement',
        revision: 0,
        confirm_automation: true,
      }),
    }))

    fireEvent.click(screen.getByRole('radio', { name: /manual review/i }))
    await waitFor(() => expect(screen.getByRole('radio', { name: /manual review/i }))
      .toHaveAttribute('aria-checked', 'true'))
    expect(screen.getByText(/Active mode:/)).toHaveTextContent('Manual review')
    expect(confirm).toHaveBeenCalledTimes(1)
  })

  it('does not enable automatic publishing when its warning is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    vi.mocked(api.apiRequest)
      .mockResolvedValueOnce(savedPreferences)
      .mockResolvedValueOnce(defaultMode)
    render(<SettingsPage appData={appData()} onDirtyChange={vi.fn()} />)

    const automatic = await screen.findByRole('radio', { name: /approve all/i })
    fireEvent.click(automatic)

    expect(automatic).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('radio', { name: /manual review/i })).toHaveAttribute('aria-checked', 'true')
    expect(vi.mocked(api.apiRequest).mock.calls.filter(([, options]) => options?.method === 'PUT')).toHaveLength(0)
  })

  it('disables publishing mode controls while the channel is disconnected', () => {
    render(<SettingsPage appData={appData(null)} onDirtyChange={vi.fn()} />)

    expect(screen.getByText('Connect a YouTube channel to manage its publishing mode.')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /manual review/i })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /approve all/i })).toBeDisabled()
    expect(api.apiRequest).not.toHaveBeenCalled()
  })
})
