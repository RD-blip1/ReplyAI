import { useEffect, useRef, useState } from 'react';
import { Bot, ChevronRight, Shield, ShieldCheck, Zap, Sparkles, FileText, ChevronDown, CheckCircle2, Eye } from 'lucide-react';
import { Avatar, PageHeader } from '../components/ui';
import './SettingsPage.css';
import AutomationControls from '../components/AutomationControls';
import TokenUsageGauge from '../components/TokenUsageGauge';
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

  const modeLabel = activeMode ? {
    manual_review: 'Manual review',
    auto_on_agreement: 'Auto-publish on agreement',
    approve_all: 'Approve all',
    dry_run: 'Dry run',
  }[activeMode.mode] : null

  return (
    <div className="page settings-page">
      <PageHeader title="Settings" subtitle="Manage your channel and reply preferences." />

      {/* Two independent columns on desktop. Below lg the column wrappers dissolve
          (display: contents) and `order` restores the row order: channel, automation,
          usage, preferences, publishing mode, AI review. */}
      <div className="settings-grid">
        <div className="settings-col">
          {/* Connected channel */}
          <section className="card card-pad settings-order-1" aria-labelledby="settings-channel">
            <h2 id="settings-channel" className="card-title mb-4">Connected channel</h2>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="flex min-w-0 items-center gap-4">
                <div className="relative shrink-0">
                  <Avatar name={session?.channel?.channel_title || 'Channel'} src={session?.channel?.channel_thumbnail_url} size={64} color="var(--accent)" />
                  {session?.connected && (
                    <span className="absolute bottom-0.5 right-0.5 h-4 w-4 rounded-full border-[3px] border-dark-800 bg-brand" aria-hidden="true" />
                  )}
                </div>
                <div className="min-w-0">
                  <h3 className="truncate text-lg font-semibold text-text-main">{session?.channel?.channel_title || 'Not connected'}</h3>
                  {session?.connected ? (
                    <span className="badge badge-green mt-2">
                      <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                      YouTube connected
                    </span>
                  ) : (
                    <div className="text-sm text-text-muted">Connect your channel to start</div>
                  )}
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (session?.connected) {
                    if (!dirty || window.confirm('Discard unsaved reply preference changes and disconnect?')) {
                      disconnectYouTube()
                    }
                  } else connectYouTube()
                }}
                className={`btn btn-lg ${session?.connected ? '' : 'btn-primary'}`}
              >
                {session?.connected ? 'Disconnect channel' : 'Connect channel'}
              </button>
            </div>
          </section>

          {/* LLM usage (gauge: teammate Hrishik1033, PR #3) */}
          <section className="card card-pad settings-order-3" aria-labelledby="settings-usage">
            <TokenUsageGauge connected={Boolean(channelId)}
              heading={<h2 id="settings-usage" className="card-title">LLM usage</h2>} />
          </section>

          {/* Publishing mode */}
          <section className="card card-pad settings-order-5" aria-labelledby="settings-mode">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <h2 id="settings-mode" className="card-title">Publishing mode</h2>
              {channelId && !loadingMode && activeMode ? (
                <p role="status" className="text-[0.9375rem] text-text-muted">
                  Active mode: <span className="font-semibold text-brand">{modeLabel}</span>
                </p>
              ) : null}
            </div>
            {loadingMode ? <p role="status" className="mb-3 text-sm text-text-muted">Loading publishing mode…</p> : null}
            {modeError ? (
              <div role="alert" className="alert alert-error mb-3">
                <span>{modeError}</span>
                {!channelId ? null : <button type="button" className="link ml-auto shrink-0" onClick={retryPublishingModeLoad}>Retry</button>}
              </div>
            ) : null}
            {!channelId ? <p className="mb-3 text-sm text-text-muted">Connect a YouTube channel to manage its publishing mode.</p> : null}
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
                    className={`mode-option ${selected ? 'is-selected' : ''}`}
                  >
                    <span className="mode-radio" aria-hidden="true" />
                    <Icon className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
                    <span className="min-w-0">
                      <span className="block text-base font-semibold text-text-main">{title}</span>
                      <span className="mt-1 block text-[0.9375rem] leading-relaxed text-text-dim">{description}</span>
                    </span>
                  </button>
                )
              })}
            </div>
            <p className="mt-4 text-sm leading-relaxed text-text-muted">
              Manual review is the only mode that always waits for a confirmation.
              The two automatic modes are checked by the same server-side rules, so switching between them changes
              how you label the intent, not which replies are allowed to post. Neither automatic mode waives any check.
            </p>
            {modeSaving ? <p role="status" className="mt-3 text-sm text-text-muted">Saving publishing mode…</p> : null}
            {modeSaved && !modeSaving && activeMode ? <p role="status" className="mt-3 flex items-center gap-2 text-sm text-brand"><CheckCircle2 className="h-4 w-4" aria-hidden="true" />Publishing mode saved.</p> : null}
            {!session?.channel?.comments_scope_available && channelId ? (
              <p className="alert alert-warning mt-3 block">
                Comment access and reply posting require youtube.force-ssl. Your channel connection will be preserved when you update permissions.{' '}
                <button
                  type="button"
                  onClick={() => connectYouTube(true)}
                  className="font-semibold underline underline-offset-2"
                >
                  Update YouTube permissions
                </button>
              </p>
            ) : null}
          </section>
        </div>

        <div className="settings-col">
          {/* Automatic operation */}
          <section className="card card-pad settings-order-2" aria-labelledby="settings-automation">
            <h2 id="settings-automation" className="card-title mb-3">Automatic operation</h2>
            {/* Loaded on demand: the controls also live on the Automation page. */}
            <button type="button" aria-expanded={showAutomation} onClick={() => setShowAutomation(value => !value)}
              className="link inline-flex items-center gap-2 text-left text-[0.9375rem]">
              <Bot className="h-4 w-4 shrink-0" aria-hidden="true" />
              {showAutomation ? 'Hide automation controls' : 'Turn automatic comment discovery on, off or pause it'}
              <ChevronRight className={`h-4 w-4 shrink-0 transition-transform ${showAutomation ? 'rotate-90' : ''}`} aria-hidden="true" />
            </button>
            {showAutomation ? <div className="mt-4 border-t border-[var(--line)] pt-4">
              <AutomationControls csrf={session?.csrf_token ?? ''} connected={Boolean(channelId)} />
            </div> : null}
          </section>

          {/* Reply Preferences */}
          <section className="card card-pad settings-order-4" aria-labelledby="settings-preferences">
            <h2 id="settings-preferences" className="card-title mb-4">Reply preferences</h2>
            {sessionLoading ? <p role="status" className="mb-4 text-sm text-text-muted">Checking channel connection…</p> : null}
            {!sessionLoading && !channelId ? (
              <p className="well mb-4 p-3 text-sm text-text-muted">
                Connect a YouTube channel to manage its reply preferences.
              </p>
            ) : null}
            {channelId && loading ? <p role="status" className="mb-4 text-sm text-text-muted">Loading saved preferences…</p> : null}
            {channelId && error ? (
              <div role="alert" className="alert alert-error mb-4 flex-col gap-2">
                <p>{error}</p>
                {saveError ? (
                  <div className="flex flex-wrap gap-3">
                    <button onClick={() => void save()} disabled={saving} className="underline disabled:opacity-50">Retry save</button>
                    <button onClick={retryLoading} disabled={saving} className="underline disabled:opacity-50">Reload saved preferences</button>
                  </div>
                ) : (
                    <button onClick={retryLoading} className="underline">Retry loading</button>
                )}
              </div>
            ) : null}

            <fieldset disabled={!channelId || loading || !saved || saving} className="space-y-5 disabled:opacity-60">
              {([
                ['Tone', 'tone', ['Professional', 'Friendly', 'Casual']],
                ['Reply length', 'reply_length', ['Short', 'Medium', 'Detailed']],
                ['Language', 'language', ['English', 'Hindi', 'Hinglish']],
              ] as const).map(([label, key, options]) => (
                <div key={key}>
                  <label className="field-label" htmlFor={`pref-${key}`}>{label}</label>
                  <div className="relative">
                    <select id={`pref-${key}`} aria-label={label} value={form[key]}
                      onChange={event => setForm(current => ({ ...current, [key]: event.target.value }))}
                      className="select">
                      {options.map(option => <option key={option}>{option}</option>)}
                    </select>
                    <ChevronDown className="pointer-events-none absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" aria-hidden="true" />
                  </div>
                </div>
              ))}

              <div>
                <label htmlFor="channel-instructions" className="field-label">Channel instructions</label>
                <textarea
                  id="channel-instructions"
                  rows={4}
                  maxLength={2000}
                  value={form.channel_instructions}
                  onChange={event => setForm(current => ({ ...current, channel_instructions: event.target.value }))}
                  className="textarea"
                  placeholder="Optional guidance for the style of replies."
                />
                <p className="mt-2 text-sm text-text-muted tabular-nums">{form.channel_instructions.length}/2000 characters</p>
              </div>
            </fieldset>
            {dirty ? <p className="mt-3 text-sm text-warning">Unsaved changes</p> : null}
            <div className="mt-5 flex flex-wrap items-center justify-end gap-3">
              {channelId && saved && !dirty && !error ? <p role="status" className="mr-auto flex items-center gap-2 text-[0.9375rem] text-brand"><CheckCircle2 className="h-4 w-4" aria-hidden="true" />Preferences saved</p> : null}
              {channelId && saving ? <p role="status" className="mr-auto text-sm text-text-muted">Saving preferences…</p> : null}
              {dirty ? (
                <button onClick={discardChanges} disabled={saving} className="btn btn-lg">
                  Discard changes
                </button>
              ) : null}
              <button onClick={() => void save()} disabled={!channelId || loading || !saved || !dirty || saving} className="btn btn-primary btn-lg">
                Save preferences
              </button>
            </div>
          </section>

          {/* AI Review */}
          <section className="card card-pad settings-order-6" aria-labelledby="settings-review">
            <h2 id="settings-review" className="card-title mb-4">AI review</h2>
            <div className="flex items-start gap-4">
              <span className="icon-tile icon-tile-green h-12 w-12" aria-hidden="true"><Sparkles /></span>
              <div className="min-w-0">
                <div className="mb-1 text-base font-semibold text-text-main">A second model checks generated replies.</div>
                <div className="text-[0.9375rem] leading-relaxed text-text-dim">
                  The secondary model independently classifies the comment and checks reply relevance, unsupported claims, and inappropriate content. Automatic modes also require at least 80% review confidence and no findings.
                </div>
              </div>
            </div>
            <div className="mt-5 border-t border-[var(--line)] pt-4">
              <h3 className="mb-3 text-base font-semibold text-text-main">Always sent to you instead of auto-published</h3>
              <ul className="grid grid-cols-1 gap-x-6 gap-y-3 text-[0.9375rem] text-text-dim sm:grid-cols-2 lg:grid-cols-1 lg:gap-y-4">
                {[
                  'Comments that try to instruct the AI',
                  'Hidden or direction-changing characters',
                  'Questions only you can answer',
                  'Possible sarcasm',
                  'Hacked accounts, refunds, legal or safety issues',
                  'Models that disagree or are unsure',
                  'Replies with links, contacts, promises or money',
                  'Spam, harassment and off-topic comments (skipped)',
                ].map(rule => <li key={rule} className="flex items-start gap-2">
                  <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-brand" aria-hidden="true" />
                  <span>{rule}</span>
                </li>)}
              </ul>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
