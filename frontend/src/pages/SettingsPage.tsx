import { useEffect, useRef, useState } from 'react';
import { Shield, Zap, Sparkles, FileText, ChevronDown, CheckCircle2, Eye } from 'lucide-react';
import AutomationControls from '../components/AutomationControls';
import { apiRequest, messageOf } from '../lib/api';
import type { PublishingMode, PublishingSettings, ReplyPreferences } from '../lib/types';
import type { useAppData } from '../hooks/useAppData';

type PreferenceValues = Omit<ReplyPreferences, 'revision'>
const defaults: PreferenceValues = {
  tone: 'Friendly',
  reply_length: 'Medium',
  language: 'English',
  channel_instructions: '',
}

// Shown before switching away from Manual review; every automatic mode needs consent.
const MODE_CONFIRMATIONS: Record<Exclude<PublishingMode, 'manual_review'>, string> = {
  dry_run: 'Dry run: ReplyAI will make every automatic decision and record what it would have posted, but it will not post anything to YouTube. Enable Dry run?',
  auto_on_agreement: 'Replies that pass all checks will be posted publicly without asking you. A reply is eligible only when triage is on-topic, both models agree, the secondary review passes every check with at least 80% confidence and no findings, and the reply-safety policy finds nothing to hold. Everything else waits for you. You can switch back to Manual review at any time. Enable Auto-publish on agreement?',
  approve_all: 'WARNING: Eligible newly generated replies may be published publicly without individual approval only when contextual triage is on-topic, both classifiers agree, and the secondary review passes every check with at least 80% confidence and no findings. Other replies stay for human review. Saved drafts will not be posted. You can switch back to Manual review at any time. Enable Approve all?',
}

export default function SettingsPage({
  appData,
  onDirtyChange,
}: {
  appData: ReturnType<typeof useAppData>
  onDirtyChange: (dirty: boolean) => void
}) {
  const { session, sessionLoading, connectYouTube, disconnectYouTube } = appData;
  const channelId = session?.connected ? session.channel?.channel_id ?? null : null
  const [saved, setSaved] = useState<ReplyPreferences | null>(null)
  const [form, setForm] = useState<PreferenceValues>(defaults)
  const [loading, setLoading] = useState(Boolean(channelId))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState(false)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const requestSequence = useRef(0)
  const saveController = useRef<AbortController | null>(null)
  const [modeResult, setModeResult] = useState<{ channelId: string; settings: PublishingSettings } | null>(null)
  const [modeLoading, setModeLoading] = useState(Boolean(channelId))
  const [modeSaving, setModeSaving] = useState(false)
  const [showAutomation, setShowAutomation] = useState(false)
  const [modeErrorResult, setModeErrorResult] = useState<{ channelId: string; message: string } | null>(null)
  const [modeSaved, setModeSaved] = useState(false)
  const modeRequestSequence = useRef(0)
  const modeSaveController = useRef<AbortController | null>(null)
  const activeMode = channelId && modeResult?.channelId === channelId ? modeResult.settings : null
  const modeError = channelId && modeErrorResult?.channelId === channelId
    ? modeErrorResult.message : null
  const loadingMode = modeLoading || Boolean(channelId && !activeMode && !modeError)
  const dirty = Boolean(channelId && saved && (
    form.tone !== saved.tone ||
    form.reply_length !== saved.reply_length ||
    form.language !== saved.language ||
    form.channel_instructions !== saved.channel_instructions
  ))

  useEffect(() => {
    onDirtyChange(dirty)
    return () => onDirtyChange(false)
  }, [dirty, onDirtyChange])

  useEffect(() => {
    if (!channelId) return
    const controller = new AbortController()
    const sequence = ++requestSequence.current
    void apiRequest<ReplyPreferences>('/api/preferences', { signal: controller.signal })
      .then((result) => {
        if (sequence !== requestSequence.current) return
        setSaved(result)
        setForm({
          tone: result.tone,
          reply_length: result.reply_length,
          language: result.language,
          channel_instructions: result.channel_instructions,
        })
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return
        if (sequence === requestSequence.current) setError(messageOf(cause, 'Preferences could not be loaded.'))
      })
      .finally(() => {
        if (sequence === requestSequence.current) setLoading(false)
      })

    return () => {
      controller.abort()
      saveController.current?.abort()
      requestSequence.current += 1
    }
  }, [channelId, loadAttempt])

  useEffect(() => {
    if (!channelId) {
      return
    }
    const controller = new AbortController()
    const sequence = ++modeRequestSequence.current
    void apiRequest<PublishingSettings>('/api/publishing-settings', { signal: controller.signal })
      .then(settings => {
        if (sequence === modeRequestSequence.current) setModeResult({ channelId, settings })
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return
        if (sequence === modeRequestSequence.current) {
          setModeErrorResult({
            channelId,
            message: messageOf(cause, 'Publishing mode could not be loaded.'),
          })
        }
      })
      .finally(() => {
        if (sequence === modeRequestSequence.current) setModeLoading(false)
      })

    return () => {
      controller.abort()
      modeSaveController.current?.abort()
      modeRequestSequence.current += 1
    }
  }, [channelId])

  async function savePublishingMode(mode: PublishingMode) {
    if (!channelId || !activeMode || modeLoading || modeSaving || mode === activeMode.mode) return
    if (mode !== 'manual_review') {
      const message = MODE_CONFIRMATIONS[mode]
      if (!window.confirm(message)) return
    }

    const controller = new AbortController()
    modeSaveController.current?.abort()
    modeSaveController.current = controller
    const sequence = ++modeRequestSequence.current
    const requestedChannelId = channelId
    setModeSaving(true)
    setModeSaved(false)
    setModeErrorResult(null)
    try {
      const settings = await apiRequest<PublishingSettings>('/api/publishing-settings', {
        method: 'PUT',
        headers: { 'X-CSRF-Token': session?.csrf_token ?? '' },
        body: JSON.stringify({
          mode,
          revision: activeMode.revision,
          confirm_automation: mode !== 'manual_review',
        }),
        signal: controller.signal,
      })
      if (sequence !== modeRequestSequence.current
        || requestedChannelId !== session?.channel?.channel_id) return
      setModeResult({ channelId: requestedChannelId, settings })
      setModeSaved(true)
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return
      if (sequence === modeRequestSequence.current) {
        setModeErrorResult({
          channelId: requestedChannelId,
          message: messageOf(cause, 'Publishing mode could not be saved.'),
        })
      }
    } finally {
      if (sequence === modeRequestSequence.current) setModeSaving(false)
    }
  }

  function retryPublishingModeLoad() {
    setModeLoading(true)
    setModeErrorResult(null)
    const sequence = ++modeRequestSequence.current
    const requestedChannelId = channelId
    void apiRequest<PublishingSettings>('/api/publishing-settings')
      .then(settings => {
        if (requestedChannelId && sequence === modeRequestSequence.current
          && requestedChannelId === session?.channel?.channel_id) {
          setModeResult({ channelId: requestedChannelId, settings })
        }
      })
      .catch((cause: unknown) => {
        if (sequence === modeRequestSequence.current) {
          setModeErrorResult({
            channelId: requestedChannelId || '',
            message: messageOf(cause, 'Publishing mode could not be loaded.'),
          })
        }
      })
      .finally(() => {
        if (sequence === modeRequestSequence.current) setModeLoading(false)
      })
  }

  async function save() {
    if (!channelId || !saved || !dirty || saving) return
    const controller = new AbortController()
    saveController.current?.abort()
    saveController.current = controller
    const sequence = ++requestSequence.current
    setSaving(true)
    setSaveError(false)
    setError(null)
    try {
      const result = await apiRequest<ReplyPreferences>('/api/preferences', {
        method: 'PUT',
        headers: { 'X-CSRF-Token': session?.csrf_token ?? '' },
        body: JSON.stringify({ ...form, revision: saved.revision }),
        signal: controller.signal,
      })
      if (sequence !== requestSequence.current || channelId !== session?.channel?.channel_id) return
      setSaved(result)
      setForm({
        tone: result.tone,
        reply_length: result.reply_length,
        language: result.language,
        channel_instructions: result.channel_instructions,
      })
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return
      if (sequence === requestSequence.current) {
        setError(messageOf(cause, 'Preferences could not be saved.'))
        setSaveError(true)
      }
    } finally {
      if (sequence === requestSequence.current) setSaving(false)
    }
  }

  function discardChanges() {
    if (!saved) return
    setForm({
      tone: saved.tone,
      reply_length: saved.reply_length,
      language: saved.language,
      channel_instructions: saved.channel_instructions,
    })
    setError(null)
    setSaveError(false)
  }

  function retryLoading() {
    setLoading(true)
    setError(null)
    setSaveError(false)
    setLoadAttempt(value => value + 1)
  }

  return (
    <div className="flex flex-col h-full gap-6 animate-in fade-in duration-300 overflow-y-auto pb-10">
      <header>
        <h1 className="text-3xl font-bold tracking-tight mb-1">Settings</h1>
        <p className="text-text-muted text-sm">Manage your channel and reply preferences.</p>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        
        {/* Left Column */}
        <div className="space-y-6">
          
          {/* Connected Channel Card */}
          <section className="rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm p-6">
            <h2 className="text-lg font-semibold mb-4">Connected channel</h2>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-4">
                <div className="relative">
                  {session?.channel?.channel_thumbnail_url ? (
                    <img src={session.channel.channel_thumbnail_url} alt="" className="w-16 h-16 rounded-full border border-dark-600" />
                  ) : (
                    <div className="w-16 h-16 rounded-full bg-dark-700 border border-dark-600" />
                  )}
                  {session?.connected && (
                    <div className="absolute bottom-0 right-0 w-4 h-4 bg-success rounded-full border-[3px] border-dark-800" />
                  )}
                </div>
                <div>
                  <h3 className="font-semibold text-lg">{session?.channel?.channel_title || 'Not connected'}</h3>
                  {session?.connected ? (
                    <div className="flex items-center gap-1.5 mt-1 px-2.5 py-0.5 rounded-full bg-success-bg border border-success/20 text-success text-xs font-medium w-fit">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      YouTube connected
                    </div>
                  ) : (
                    <div className="text-sm text-text-muted">Connect your channel to start</div>
                  )}
                </div>
              </div>
              <button 
                onClick={() => {
                  if (session?.connected) {
                    if (!dirty || window.confirm('Discard unsaved reply preference changes and disconnect?')) {
                      disconnectYouTube()
                    }
                  } else connectYouTube()
                }}
                className="px-4 py-2 rounded-lg border border-dark-600 bg-dark-900/50 hover:bg-dark-700 text-sm font-medium transition-colors"
              >
                {session?.connected ? 'Disconnect channel' : 'Connect channel'}
              </button>
            </div>
          </section>

          {/* Reply Preferences */}
          <section className="rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm p-6">
            <h2 className="text-lg font-semibold mb-4">Reply preferences</h2>
            {sessionLoading ? <p role="status" className="mb-4 text-sm text-text-muted">Checking channel connection…</p> : null}
            {!sessionLoading && !channelId ? (
              <p className="p-3 mb-4 rounded-lg bg-dark-900 border border-dark-600 text-sm text-text-muted">
                Connect a YouTube channel to manage its reply preferences.
              </p>
            ) : null}
            {channelId && loading ? <p role="status" className="mb-4 text-sm text-text-muted">Loading saved preferences…</p> : null}
            {channelId && error ? (
              <div role="alert" className="p-3 mb-4 rounded-lg bg-error-bg border border-error/20 text-error text-sm">
                <p>{error}</p>
                {saveError ? (
                  <div className="flex flex-wrap gap-3 mt-2">
                    <button onClick={() => void save()} disabled={saving} className="underline disabled:opacity-50">Retry save</button>
                    <button onClick={retryLoading} disabled={saving} className="underline disabled:opacity-50">Reload saved preferences</button>
                  </div>
                ) : (
                    <button onClick={retryLoading} className="mt-2 underline">Retry loading</button>
                )}
              </div>
            ) : null}
            
            <fieldset disabled={!channelId || loading || !saved || saving} className="space-y-4 disabled:opacity-60">
              <div>
                <label className="block text-sm text-text-main mb-1.5">Tone</label>
                <div className="relative">
                  <select aria-label="Tone" value={form.tone} onChange={event => setForm(current => ({ ...current, tone: event.target.value as PreferenceValues['tone'] }))} className="w-full appearance-none bg-dark-900 border border-dark-600 rounded-lg px-4 py-2.5 text-sm text-text-main focus:outline-none focus:border-brand">
                    <option>Professional</option><option>Friendly</option><option>Casual</option>
                  </select>
                  <ChevronDown className="w-4 h-4 absolute right-3 top-1/2 -translate-y-1/2 text-text-muted pointer-events-none" />
                </div>
              </div>

              <div>
                <label className="block text-sm text-text-main mb-1.5">Reply length</label>
                <div className="relative">
                  <select aria-label="Reply length" value={form.reply_length} onChange={event => setForm(current => ({ ...current, reply_length: event.target.value as PreferenceValues['reply_length'] }))} className="w-full appearance-none bg-dark-900 border border-dark-600 rounded-lg px-4 py-2.5 text-sm text-text-main focus:outline-none focus:border-brand">
                    <option>Short</option><option>Medium</option><option>Detailed</option>
                  </select>
                  <ChevronDown className="w-4 h-4 absolute right-3 top-1/2 -translate-y-1/2 text-text-muted pointer-events-none" />
                </div>
              </div>

              <div>
                <label className="block text-sm text-text-main mb-1.5">Language</label>
                <div className="relative">
                  <select aria-label="Language" value={form.language} onChange={event => setForm(current => ({ ...current, language: event.target.value as PreferenceValues['language'] }))} className="w-full appearance-none bg-dark-900 border border-dark-600 rounded-lg px-4 py-2.5 text-sm text-text-main focus:outline-none focus:border-brand">
                    <option>English</option><option>Hindi</option><option>Hinglish</option>
                  </select>
                  <ChevronDown className="w-4 h-4 absolute right-3 top-1/2 -translate-y-1/2 text-text-muted pointer-events-none" />
                </div>
              </div>

              <div>
                <label htmlFor="channel-instructions" className="block text-sm text-text-main mb-1.5">Channel instructions</label>
                <textarea
                  id="channel-instructions"
                  rows={3}
                  maxLength={2000}
                  value={form.channel_instructions}
                  onChange={event => setForm(current => ({ ...current, channel_instructions: event.target.value }))}
                  className="w-full bg-dark-900 border border-dark-600 rounded-lg p-3 text-sm text-text-main focus:outline-none focus:border-brand resize-y max-w-full"
                  placeholder="Optional guidance for the style of replies."
                />
                <p className="mt-1 text-xs text-text-muted">{form.channel_instructions.length}/2000 characters</p>
              </div>
            </fieldset>
            {dirty ? <p className="mt-3 text-xs text-warning">Unsaved changes</p> : null}
            <div className="flex flex-wrap items-center justify-end gap-3 mt-5">
              {dirty ? (
                <button onClick={discardChanges} disabled={saving} className="px-4 py-2 rounded-lg border border-dark-600 text-sm font-medium disabled:opacity-50">
                  Discard changes
                </button>
              ) : null}
              {channelId && saved && !dirty && !error ? <p role="status" className="mr-auto text-sm text-success">Preferences saved</p> : null}
              {channelId && saving ? <p role="status" className="mr-auto text-sm text-text-muted">Saving preferences…</p> : null}
              <button onClick={() => void save()} disabled={!channelId || loading || !saved || !dirty || saving} className="px-5 py-2.5 rounded-lg bg-brand hover:bg-brand-dark text-white text-sm font-semibold transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                Save preferences
              </button>
            </div>
          </section>
        </div>

        {/* Right Column */}
        <div className="space-y-6">
          
          {/* Automatic operation */}
          <section className="rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm p-6">
            <h2 className="text-lg font-semibold mb-4">Automatic operation</h2>
            {/* Loaded on demand: the controls also live on the Automation page. */}
            <button type="button" aria-expanded={showAutomation} onClick={() => setShowAutomation(value => !value)}
              className="text-sm text-text-muted underline">
              {showAutomation ? 'Hide automation controls' : 'Turn automatic comment discovery on, off or pause it'}
            </button>
            {showAutomation ? <div className="mt-3">
              <AutomationControls csrf={session?.csrf_token ?? ''} connected={Boolean(channelId)} />
            </div> : null}
          </section>

          {/* Publishing mode */}
          <section className="rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm p-6">
            <h2 className="text-lg font-semibold mb-4">Publishing mode</h2>
            {loadingMode ? <p role="status" className="mb-3 text-sm text-text-muted">Loading publishing mode…</p> : null}
            {modeError ? (
              <div role="alert" className="mb-3 text-sm text-danger">
                {modeError}
                {!channelId ? null : <button type="button" className="ml-2 underline" onClick={retryPublishingModeLoad}>Retry</button>}
              </div>
            ) : null}
            {!channelId ? <p className="mb-3 text-sm text-text-muted">Connect a YouTube channel to manage its publishing mode.</p> : null}
            {channelId && !loadingMode && activeMode ? (
              <p role="status" className="mb-3 text-sm text-text-muted">
                Active mode: <span className="font-semibold text-text-main">{{
                  manual_review: 'Manual review',
                  auto_on_agreement: 'Auto-publish on agreement',
                  approve_all: 'Approve all',
                  dry_run: 'Dry run',
                }[activeMode.mode]}</span>
              </p>
            ) : null}
            <div role="radiogroup" aria-label="Publishing mode" className="space-y-3">
              {([
                {
                  mode: 'manual_review',
                  title: 'Manual review',
                  description: 'Nothing posts automatically. Every reply stays a draft until you review it, approve it, and confirm each publication.',
                  Icon: FileText,
                },
                {
                  mode: 'auto_on_agreement',
                  title: 'Auto-publish on agreement',
                  description: 'Newly generated replies may post without a per-reply confirmation only when triage is on-topic, both classifiers agree, and the secondary review passes every check with at least 80% confidence and no findings. Everything else still waits for you.',
                  Icon: Zap,
                },
                {
                  mode: 'approve_all',
                  title: 'Approve all',
                  description: 'Also lets eligible newly generated replies post without a per-reply confirmation. This is not a blanket approval: it waives no check, so the same triage, classifier-agreement, confidence, and findings rules apply, and anything that fails one stays for human review.',
                  Icon: Shield,
                },
                {
                  mode: 'dry_run',
                  title: 'Dry run',
                  description: 'Makes every automatic decision and records what would have been posted, without posting anything. Use it to see how automation would behave on your comments.',
                  Icon: Eye,
                },
              ] as const).map(({ mode, title, description, Icon }) => {
                const selected = activeMode?.mode === mode
                return (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    disabled={!channelId || loadingMode || !activeMode || modeSaving || selected}
                    onClick={() => void savePublishingMode(mode)}
                    className={`flex w-full items-start gap-4 p-4 rounded-xl border text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                      selected ? 'border-brand bg-brand/5' : 'border-dark-600 bg-dark-900/50 hover:border-dark-500'
                    }`}
                  >
                    <span className={`mt-1 w-5 h-5 shrink-0 rounded-full border-2 ${selected ? 'border-[5px] border-brand bg-dark-900' : 'border-dark-500'}`} />
                    <Icon className={`w-6 h-6 shrink-0 ${selected ? 'text-brand-light' : 'text-text-muted'}`} />
                    <span>
                      <span className="block font-semibold text-text-main">{title}</span>
                      <span className="block text-sm text-text-muted">{description}</span>
                    </span>
                  </button>
                )
              })}
            </div>
            <p className="mt-3 text-xs text-text-muted">
              What&apos;s the difference? Manual review is the only mode that always waits for a confirmation.
              The two automatic modes are checked by the same server-side rules, so switching between them changes
              how you label the intent, not which replies are allowed to post. Neither automatic mode waives any check.
            </p>
            {modeSaving ? <p role="status" className="mt-3 text-sm text-text-muted">Saving publishing mode…</p> : null}
            {modeSaved && !modeSaving && activeMode ? <p role="status" className="mt-3 text-sm text-success">Publishing mode saved.</p> : null}
            {!session?.channel?.comments_scope_available && channelId ? (
              <p className="mt-3 text-sm text-warning">
                Comment access and reply posting require youtube.force-ssl. Your channel connection will be preserved when you update permissions.{' '}
                <button
                  type="button"
                  onClick={() => connectYouTube(true)}
                  className="underline font-medium hover:text-warning/80"
                >
                  Update YouTube permissions
                </button>
              </p>
            ) : null}
          </section>

          {/* AI Review */}
          <section className="rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm p-6">
            <h2 className="text-lg font-semibold mb-4">AI review</h2>
            <div className="flex items-start gap-4">
              <Sparkles className="w-8 h-8 text-brand-light flex-shrink-0" />
              <div>
                <div className="font-semibold text-text-main mb-1">A second model checks generated replies.</div>
                <div className="text-sm text-text-muted leading-relaxed">
                  The secondary model independently classifies the comment and checks reply relevance, unsupported claims, and inappropriate content. Automatic modes also require at least 80% review confidence and no findings.
                </div>
              </div>
            </div>
          </section>

          {/* Usage this month */}
          <section className="rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm p-6">
            <h2 className="text-lg font-semibold mb-4">Usage this month</h2>
            <div className="flex items-center justify-center p-6 border border-dark-600 border-dashed rounded-xl bg-dark-900/50">
              <div className="text-sm text-text-muted text-center max-w-sm">
                Aggregate usage history is not supported by the current backend API. (Values omitted).
              </div>
            </div>
            
            <div className="flex items-center justify-end gap-3 mt-6">
              <button disabled className="px-5 py-2.5 rounded-lg border border-dark-600 bg-transparent text-sm font-medium opacity-50 cursor-not-allowed">
                Cancel
              </button>
              <button disabled className="px-5 py-2.5 rounded-lg bg-brand hover:bg-brand-dark text-white text-sm font-semibold transition-colors opacity-50 cursor-not-allowed">
                Save changes
              </button>
            </div>
          </section>

        </div>
      </div>
    </div>
  );
}
