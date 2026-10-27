import { useEffect, useRef, useState } from 'react'

type Classification = { sentiment: string; intent: string }
type StageUsage = { model: string; input_tokens: number | null; output_tokens: number | null }
type Draft = {
  id: string; text: string; revision: number; original_comment: string
  video_id: string; comment_id: string
  primary_classification: Classification | null; secondary_classification: Classification | null
  triage?: { category: string; confidence: number } | null
  agreement: string; generation_status: string; review_status: string; human_decision: string
  quality: { relevance: string; unsupported_claims: string; inappropriate_content: string; confidence?: number; findings: string[] } | null
  // Stage codes, plus worker metadata such as _worker_retry or retry_after_seconds.
  failure: Record<string, unknown>; providers: Record<string, { model: string }>
  token_usage: Partial<Record<'generation' | 'classification' | 'quality', StageUsage>>
}
type UsageEntry = {
  revision: number; operation: string; stage: string; role: string; attempt: number
  model: string; input_tokens: number | null; output_tokens: number | null
  consumption: 'reported' | 'unknown'; applied: boolean
}
type UsageCoverage = {
  complete: boolean; pre_ledger_activity: boolean; unbacked_snapshot_stages: string[]
  notice: string | null; unknown_notice: string | null
}
type UsageHistory = {
  entries: UsageEntry[]
  totals: { requests: number; reported_requests: number; unknown_requests: number
            discarded_requests: number; input_tokens: number; output_tokens: number }
  coverage: UsageCoverage
}
type Setup = { configured: boolean; missing: string[]; invalid: string[] }
type Publication = {
  id: string; operation_id: string; status: 'posted' | 'unknown' | 'failed'
  channel_id: string; video_id: string; parent_comment_id: string
  draft_revision: number; text: string; reply_comment_id: string | null
  failure: Record<string, string>; sent_at: string | null
  created_at: string | null; resolved_at: string | null; retryable: boolean
}
type Eligibility = {
  eligible: boolean; blocked_code: string | null; blocked_message: string | null
  scope_available: boolean; channel_title: string; revision: number
  review: { review_status: string; checks: Record<string, string | null>
            confidence: number | null; findings: string[]; agreement: string
            policy?: { rule: string; level: string; detail: string }[] }
}
type PublicationState = { publication: Publication | null; eligibility: Eligibility }
type Turn = {
  id: string; base_revision: number; instruction: string; suggestion: string
  status: 'suggested' | 'applied' | 'unchanged' | 'discarded' | 'stale'
  created_at: string | null
}
const apiBase = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8001').replace(/\/$/, '')
const stageLabels: Record<string, string> = {
  generation: 'Generation', classification: 'Classification', quality: 'Quality review',
  refinement: 'Reply refinement',
}
// Only string-valued stage codes are shown; worker bookkeeping (objects, numbers and
// underscore-prefixed keys) shares the failure map and is not user-facing.
function stageFailures(failure: Record<string, unknown> | null | undefined): [string, string][] {
  return Object.entries(failure ?? {}).filter(
    (entry): entry is [string, string] => !entry[0].startsWith('_') && typeof entry[1] === 'string')
}
const failureMessages: Record<string, string> = {
  video_unavailable: 'This video is no longer in your channel uploads.',
  comment_not_returned: 'YouTube did not return this comment. It may be deleted or still under review.',
  comment_id_mismatch: 'YouTube returned a different comment than requested. Reload the comment list.',
  comment_video_mismatch: 'This comment belongs to a different video.',
  comment_video_unverifiable: 'YouTube did not report which video this comment belongs to, so it was rejected.',
  comment_channel_mismatch: 'This comment is not on one of your channel’s videos.',
  comment_is_reply: 'Replies cannot be drafted. Open the top-level comment instead.',
  comment_text_unavailable: 'This comment has no readable text.',
  comment_unavailable: 'Comment is unavailable for this video.',
  rate_limited: 'The provider rate limit was reached. Wait a moment, then retry this step.',
  provider_unavailable: 'The provider did not respond. Retry this step.',
  provider_timeout: 'The provider took too long. Retry this step.',
  provider_rejected_request: 'The provider rejected the request. Check the API key and model settings.',
  provider_request_failed: 'The provider request failed. Retry this step.',
  invalid_response: 'The model returned an unusable response. Retry this step.',
  incomplete_response: 'The model response was cut short. Retry this step.',
  transient_error: 'The provider was temporarily unavailable. Retry this step.',
  generation_required: 'Generate or save reply text first.',
  stale_draft: 'The saved draft changed. Reload it, then refine again.',
  stale_suggestion: 'The saved draft changed after that suggestion. Refine again against the current reply.',
  suggestion_discarded: 'That suggestion was discarded.',
  suggestion_applied: 'That suggestion was already applied.',
  suggestion_unavailable: 'That suggestion is no longer available.',
  draft_conflict: 'The draft changed or is busy. Reload the saved state and retry.',
  ai_setup_required: 'AI setup required. Configure both models on the backend.',
}
const qualityCheckLabels = [
  ['relevance', 'Relevance'],
  ['unsupported_claims', 'Unsupported claims'],
  ['inappropriate_content', 'Inappropriate content'],
] as const
const turnStatus: Record<Turn['status'], string> = {
  suggested: 'Suggested — not applied',
  applied: 'Applied',
  unchanged: 'Identical to the saved reply — no change made',
  discarded: 'Discarded',
  stale: 'Out of date — the saved draft changed after this was suggested',
}
const count = (value: number | null) => (value === null ? 'unknown' : value.toLocaleString())
const operationLabels: Record<string, string> = {
  generate: 'Initial generation', regenerate: 'Regeneration', review: 'Re-review', edit: 'Edit',
  refine: 'Reply refinement',
}
const roleLabels: Record<string, string> = { primary: 'primary', secondary: 'secondary' }
const publicationStatus: Record<Publication['status'], string> = {
  posted: 'Posted to YouTube',
  unknown: 'Outcome unknown — check YouTube before trying again',
  failed: 'Not posted',
}

function CurrentStageUsage({ usage }: { usage: Draft['token_usage'] }) {
  const stages = Object.entries(usage ?? {})
  return <details className="draft-usage">
    <summary>Latest step token usage</summary>
    {stages.length ? <dl>{stages.map(([stage, entry]) => <div key={stage}>
      <dt>{stageLabels[stage] || stage} ({entry?.model || 'unconfigured'})</dt>
      <dd>Input {count(entry?.input_tokens ?? null)} · Output {count(entry?.output_tokens ?? null)}</dd>
    </div>)}</dl> : <p>No step has reported usage yet.</p>}
    <p>This panel shows only the most recent call for each stage. Later steps overwrite it,
      and saving an edit clears the stages it invalidated. It is not a running total.</p>
    <p>Counts are what the provider reported. Unknown means it was not reported, not zero.</p>
  </details>
}

function UsageLedger({ history }: { history: UsageHistory | null }) {
  if (!history) return null
  const { entries, totals, coverage } = history
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
  return <details className="draft-usage">
    <summary>{coverage.complete ? 'Cumulative usage' : 'Usage recorded since tracking began'} ·
      {plural(totals.requests, 'model request', 'model requests')}</summary>
    {coverage.notice && <p role="status">{coverage.notice}</p>}
    {coverage.unbacked_snapshot_stages.length > 0 && <p>These steps reported usage that is not in
      the list below: {coverage.unbacked_snapshot_stages.map((stage) => stageLabels[stage] || stage).join(', ')}.</p>}
    <p>Reported input {count(totals.input_tokens)} · reported output {count(totals.output_tokens)},
      summed over the requests listed below. A step that reuses a stored result without calling
      the provider is not counted, and one that is later discarded still is, because the request
      happened either way.</p>
    {totals.unknown_requests > 0 && <p>{plural(totals.unknown_requests, 'request reported',
        'requests reported')} no token counts. {coverage.unknown_notice}</p>}
    {totals.discarded_requests > 0 && <p>{plural(totals.discarded_requests, 'request was',
        'requests were')} made by an operation that a stale-revision check later rejected, so its
      result was not saved to the draft. The usage still happened and is counted here.</p>}
    {entries.length ? <ol className="draft-usage-log">{entries.map((entry, index) => <li key={index}>
      Revision {entry.revision} · {operationLabels[entry.operation] || entry.operation} ·
      {' '}{stageLabels[entry.stage] || entry.stage} ({roleLabels[entry.role] || entry.role}
      {entry.model ? `, ${entry.model}` : ''}){entry.attempt > 1 ? `, attempt ${entry.attempt}` : ''}
      {entry.applied ? '' : ' — discarded, not saved'} — input {count(entry.input_tokens)},
      {' '}output {count(entry.output_tokens)}
    </li>)}</ol> : <p>No model requests have been recorded for this comment yet.</p>}
    <p>Counts are what the provider reported. Unknown means no readable response came back, so it
      is not recorded as zero and is not a confirmed charge.</p>
  </details>
}

const messageOf = (cause: unknown, fallback: string) =>
  cause instanceof Error ? cause.message : fallback

// A request must always settle. Without a deadline a connection that never comes back
// leaves the panel showing a permanent "Asking the model…" with no way to recover.
const DEFAULT_DEADLINE_MS = 15_000
// The backend settles a model stage inside its own 95s budget, so this is a backstop for
// a dead connection, not a race to beat the server. It must stay longer than the server's
// budget: a request abandoned early may still have produced a suggestion, so the client
// must not give up before the server has said what happened.
const REFINEMENT_DEADLINE_MS = 120_000
// Generation, regeneration, and re-review can run several model stages server-side
// (triage, primary generation, secondary classification, quality review), each with its
// own 95s budget. This backstop must outlast the whole sequence, not one stage: aborting
// early would leave the server still working while the panel reports a timeout.
const MODEL_DEADLINE_MS = 300_000
// A publish makes a real YouTube insert whose own HTTP budget is 10s, so this must be
// comfortably longer than that. It must not be short: a request abandoned early may
// still have been posted, and giving up would invite a second reply to the same comment.
const PUBLISH_DEADLINE_MS = 30_000
const TIMED_OUT =
  'The request did not finish in time. The server may still have completed it, so select ' +
  '“Reload saved state” to pick up any saved result without sending a new request.'

async function request<T>(path: string, options: RequestInit & { deadlineMs?: number } = {}): Promise<T> {
  const { deadlineMs = DEFAULT_DEADLINE_MS, signal: outer, ...rest } = options
  const controller = new AbortController()
  const forward = () => controller.abort()
  if (outer) {
    if (outer.aborted) forward()
    else outer.addEventListener('abort', forward, { once: true })
  }
  const expired = { value: false }
  const timer = window.setTimeout(() => { expired.value = true; controller.abort() }, deadlineMs)
  try {
    const response = await fetch(`${apiBase}/api/drafts${path}`, {
      ...rest, signal: controller.signal, credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...rest.headers },
    })
    const value = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = value?.detail
      const fallback = 'Draft request failed. Reload saved state and retry.'
      const message = typeof detail === 'object' && detail
        ? (detail.message || fallback)
        : (typeof detail === 'string' && detail) || fallback
      const code = typeof detail === 'object' && detail ? detail.code : null
      throw new Error(code ? `${message} (code: ${code})` : message)
    }
    return value as T
  } catch (cause) {
    if (expired.value) throw new Error(TIMED_OUT)
    throw cause
  } finally {
    window.clearTimeout(timer)
    outer?.removeEventListener('abort', forward)
  }
}

function RefinementConversation({ draft, csrf, dirty, configured, blocked, onApplied }: {
  draft: Draft
  csrf: string
  dirty: boolean
  configured: boolean
  blocked: boolean
  onApplied: (saved: Draft) => void
}) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const lock = useRef(false)
  const mounted = useRef(true)
  // Set the flag in the effect body, not only in its cleanup. StrictMode mounts, unmounts,
  // and remounts every component in development, so a cleanup-only flag is left false
  // afterwards and every guarded setState below is skipped forever: the spinner never
  // clears, the suggestion never appears, and the conversation never finishes loading.
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  // History comes from the server, so it survives a refresh and an authorized
  // same-channel reconnection. Reading it costs no model call.
  useEffect(() => {
    let active = true
    request<{ turns: Turn[] }>(`/${draft.id}/conversation`)
      .then(value => { if (active) setTurns(value.turns) })
      .catch(cause => { if (active) setError(messageOf(cause, 'Could not load the refinement conversation.')) })
      .finally(() => { if (active) setLoaded(true) })
    return () => { active = false }
  }, [draft.id, draft.revision])

  async function send() {
    if (lock.current || !instruction.trim()) return
    lock.current = true
    setBusy('Asking the model for a suggested reply…')
    setError(null)
    try {
      const result = await request<{ turn: Turn; draft: Draft }>(`/${draft.id}/refine`, {
        method: 'POST', headers: { 'X-CSRF-Token': csrf },
        deadlineMs: REFINEMENT_DEADLINE_MS,
        body: JSON.stringify({ revision: draft.revision, instruction: instruction.trim() }),
      })
      if (mounted.current) {
        setTurns(previous => [...previous, result.turn])
        setInstruction('')
      }
    } catch (cause) {
      // A failed refinement leaves the saved draft and its approval exactly as they were.
      if (mounted.current) setError(messageOf(cause, 'The refinement could not be produced.'))
    } finally {
      lock.current = false
      if (mounted.current) setBusy('')
    }
  }

  async function resolve(turn: Turn, action: 'apply' | 'discard') {
    if (lock.current) return
    lock.current = true
    setBusy(action === 'apply' ? 'Applying the suggestion…' : 'Discarding the suggestion…')
    setError(null)
    try {
      if (action === 'apply') {
        const saved = await request<Draft>(`/${draft.id}/refine/${turn.id}/apply`, {
          method: 'POST', headers: { 'X-CSRF-Token': csrf },
          body: JSON.stringify({ revision: draft.revision }),
        })
        if (mounted.current) onApplied(saved)
        if (mounted.current) {
          setTurns(previous => previous.map(item => item.id === turn.id
            ? { ...item, status: saved.text.trim() === item.suggestion.trim() ? 'unchanged' : 'applied' }
            : item))
        }
      } else {
        const updated = await request<Turn>(`/${draft.id}/refine/${turn.id}/discard`, {
          method: 'POST', headers: { 'X-CSRF-Token': csrf },
        })
        if (mounted.current) setTurns(previous => previous.map(item => item.id === turn.id ? updated : item))
      }
    } catch (cause) {
      if (mounted.current) setError(messageOf(cause, 'That suggestion could not be updated.'))
    } finally {
      lock.current = false
      if (mounted.current) setBusy('')
    }
  }

  const ordered = [...turns].reverse()
  const fieldId = `refinement-instruction-${draft.id}`
  const inputBlocked = Boolean(busy) || blocked
  // While a request is in flight its own message already explains why, so a reason is
  // only worth stating when nothing else is being shown.
  const inputReason = busy ? null : 'Wait for the saved draft to finish loading.'
  const sendBlocked = inputBlocked || !configured || dirty || !instruction.trim()
  const sendReason = !configured
    ? 'AI setup is incomplete, so refinement is unavailable.'
    : dirty
      ? 'Save or discard your unsaved edits first.'
      : 'Type an instruction to enable Send.' 
  return <details className="refinement group min-w-0" open>
    <summary className="font-semibold text-text-main cursor-pointer outline-none group-hover:text-brand transition-colors">Refine with AI</summary>
    
    <div className="mt-3 text-sm text-text-muted mb-3 flex items-center justify-between">
      <span>AI suggestions are local. Applying one creates a new revision.</span>
      {dirty && <span className="text-warning">Save edits first to refine.</span>}
    </div>

    <div className="flex flex-col gap-2">
      <textarea
        id={fieldId}
        aria-label="Instruction, such as 'make it warmer'"
        className="w-full bg-dark-900 border border-dark-600 rounded-lg p-3 text-sm text-text-main placeholder:text-text-dim focus:border-brand focus:ring-1 focus:ring-brand outline-none transition-all resize-none min-h-[80px]"
        value={instruction}
        placeholder="Instruction, e.g., 'make it warmer'"
        maxLength={500}
        disabled={inputBlocked}
        onChange={event => setInstruction(event.target.value)}
      />
      <div className="flex items-center justify-between mt-1">
        <span className="text-xs text-text-dim">{sendReason || inputReason}</span>
        <button 
          type="button" 
          disabled={sendBlocked} 
          onClick={() => void send()}
          className="px-4 py-1.5 bg-brand hover:bg-brand-light text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
        >
          Send instruction
        </button>
      </div>
    </div>

    <div className="mt-4 space-y-3 refinement-history">
      {loaded && !error && ordered.length === 0 && <p className="text-sm text-text-dim">No refinement instructions yet.</p>}
      {busy && <p className="text-sm text-brand animate-pulse">{busy}</p>}
      {error && <p className="text-sm text-error" role="alert">{error}</p>}
      {ordered.map(turn => {
        const stale = turn.status === 'suggested' && turn.base_revision !== draft.revision
        const status = stale ? 'stale' : turn.status
        return <article className={`p-3 rounded-lg border border-dark-600 bg-dark-900/50 ${status}`} key={turn.id}>
          <div className="text-sm font-medium text-text-main mb-2">You: {turn.instruction}</div>
          {status === 'stale' && <p className="text-xs text-warning mb-2" role="status">
            Stale suggestion from revision {turn.base_revision}; the saved reply is now revision {draft.revision}.
            Ask for a new suggestion based on the current reply.
          </p>}
          {turn.suggestion && <div className="mt-2 pl-3 border-l-2 border-brand/30">
            <div className="text-sm text-text-muted mb-3">{turn.suggestion}</div>
            {status !== 'suggested' && status !== 'stale' && (
              <div className="text-xs text-text-dim mb-2 italic" role="status">
                {turnStatus[status]}
              </div>
            )}
            {status === 'suggested' && (
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={blocked || dirty}
                  onClick={() => void resolve(turn, 'apply')}
                  className="px-3 py-1 bg-dark-800 border border-dark-600 hover:bg-dark-700 rounded text-xs font-medium transition-colors disabled:opacity-50"
                >
                  Apply suggestion
                </button>
                <button
                  type="button"
                  disabled={blocked || dirty}
                  onClick={() => void resolve(turn, 'discard')}
                  className="px-3 py-1 bg-dark-800 border border-dark-600 hover:bg-dark-700 rounded text-xs font-medium transition-colors disabled:opacity-50"
                >
                  Discard suggestion
                </button>
              </div>
            )}
          </div>}
        </article>
      })}
      {!loaded && !error && <p className="text-sm text-text-muted">Loading conversation...</p>}
    </div>
  </details>
}

type Reconciliation = { state: string; candidates: { reply_comment_id: string }[]; scan_complete: boolean }

// An unknown outcome is resolved only by a person. "Check YouTube" reads the replies
// and reports a candidate; it never marks anything posted and never sends a reply.
function Reconcile({ draft, csrf, onResolved }: {
  draft: Draft; csrf: string; onResolved: (state: PublicationState) => void
}) {
  const [seen, setSeen] = useState<{ reconciliation: Reconciliation; message: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function check() {
    setBusy(true); setError(null)
    try {
      setSeen(await request(`/${draft.id}/publication/check`, { method: 'POST', headers: { 'X-CSRF-Token': csrf } }))
    } catch (cause) {
      setError(messageOf(cause, 'YouTube could not be checked.'))
    } finally { setBusy(false) }
  }

  async function resolve(resolution: 'posted' | 'not_posted') {
    const prompt = resolution === 'posted'
      ? 'Confirm that this reply is on YouTube. ReplyAI will record it as posted.'
      : 'Confirm that this reply is NOT on YouTube. ReplyAI will allow posting it again.'
    if (!window.confirm(prompt)) return
    setBusy(true); setError(null)
    try {
      const candidates = seen?.reconciliation.candidates ?? []
      await request(`/${draft.id}/publication/reconcile`, {
        method: 'POST', headers: { 'X-CSRF-Token': csrf },
        body: JSON.stringify({ resolution, ...(candidates.length === 1 ? { reply_comment_id: candidates[0].reply_comment_id } : {}) }),
      })
      onResolved(await request<PublicationState>(`/${draft.id}/publication`))
    } catch (cause) {
      setError(messageOf(cause, 'The outcome could not be recorded.'))
    } finally { setBusy(false) }
  }

  return <div className="draft-actions" aria-label="Reconcile unknown outcome">
    <button type="button" disabled={busy} onClick={() => void check()}>Check YouTube</button>
    {seen && <p role="status">{seen.message}</p>}
    {seen?.reconciliation.state === 'external_match_found' &&
      <button type="button" disabled={busy} onClick={() => void resolve('posted')}>It is on YouTube</button>}
    {seen && <button type="button" disabled={busy} onClick={() => void resolve('not_posted')}>It is not on YouTube</button>}
    {error && <p className="draft-error" role="alert">{error}</p>}
  </div>
}

function PublishReply({ draft, csrf, state, dirty, blocked, onPublished }: {
  draft: Draft
  csrf: string
  state: PublicationState | null
  dirty: boolean
  blocked: boolean
  onPublished: (state: PublicationState) => void
}) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lock = useRef(false)
  const mounted = useRef(true)
  const trigger = useRef<HTMLButtonElement | null>(null)
  const cancel = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  // Focus has to move into the dialog when it opens, come back to the button that opened
  // it when it closes, and Escape has to dismiss it. Without this the dialog is visible
  // but unreachable by keyboard, and the destructive Confirm button is still in the tab
  // order behind it.
  useEffect(() => {
    if (!confirming) return
    cancel.current?.focus()
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape' || lock.current) return
      event.preventDefault()
      setConfirming(false)
      setError(null)
      trigger.current?.focus()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [confirming])

  const record = state?.publication ?? null
  const eligible = Boolean(state?.eligibility?.eligible)

  async function confirm() {
    if (lock.current) return
    lock.current = true
    setBusy(true)
    setError(null)
    try {
      // The server reads the text, the approval, the review, and the ownership itself.
      // The revision and text sent here only prove this confirmation matched what is
      // saved, so a reply edited in another tab cannot be published by accident.
      await request<Publication>(`/${draft.id}/publication`, {
        method: 'POST', headers: { 'X-CSRF-Token': csrf },
        deadlineMs: PUBLISH_DEADLINE_MS,
        body: JSON.stringify({ revision: draft.revision, text: draft.text }),
      })
      const fresh = await request<PublicationState>(`/${draft.id}/publication`)
      if (mounted.current) { setConfirming(false); onPublished(fresh) }
    } catch (cause) {
      // The server records what it actually knows, including an unknown outcome when a
      // request was sent but no answer came back. That durable state is better than the
      // client's guess, so it wins whenever a record exists. The raw message is only
      // shown when nothing was recorded, which is the case for a pre-send refusal.
      const message = messageOf(cause, 'The reply could not be posted.')
      try {
        const fresh = await request<PublicationState>(`/${draft.id}/publication`)
        if (fresh.publication) {
          if (mounted.current) { setConfirming(false); onPublished(fresh) }
          return
        }
      } catch { /* The state is unreadable, so fall back to the failure itself. */ }
      if (mounted.current) setError(message)
    } finally {
      lock.current = false
      if (mounted.current) setBusy(false)
    }
  }

  // Only a definitive rejection posted nothing, so only that state offers another try.
  // An unknown outcome is never offered one, because a repeat risks two public replies.
  if (record?.status === 'posted' || record?.status === 'unknown') {
    return <section className="publish-state" aria-label="Posting result">
      <p className={`publish-badge ${record.status}`}>{publicationStatus[record.status]}</p>
      <p>Reply for saved revision {record.draft_revision}:</p>
      <blockquote className="publish-text">{record.text}</blockquote>
      {record.status === 'posted'
        ? <p>YouTube confirmed this reply{record.reply_comment_id ? ` with comment id ${record.reply_comment_id}` : ''}.
            It is public and cannot be un-posted by replying again from this draft.</p>
        : <p>ReplyAI asked YouTube to post this reply and cannot tell whether it arrived, so it
            will not send it again. Open the video’s replies and check. This draft stays blocked
            until you confirm the outcome there.</p>}
      {record.status === 'unknown' && <Reconcile draft={draft} csrf={csrf} onResolved={onPublished} />}
    </section>
  }

  return <>
    <div className="draft-actions">
      <button
        ref={trigger}
        type="button"
        disabled={blocked || dirty || !state || !eligible || busy}
        onClick={() => { setError(null); setConfirming(true) }}
      >Post reply to YouTube…</button>
    </div>
    {!dirty && state && !eligible && state.eligibility?.blocked_message &&
      <p className="refinement-reason" role="status">{state.eligibility.blocked_message}</p>}
    {dirty && <p className="refinement-reason" role="status">
      Posting is paused while you have unsaved edits, so the reply that goes out is the exact
      one you reviewed.</p>}
    {record?.status === 'failed' && <section className="publish-state" aria-label="Posting result">
      <p className="publish-badge failed">{publicationStatus.failed}</p>
      <p>{record.failure?.message || 'No reply was posted.'}</p>
      <p>Nothing was posted, so you may try again. Nothing is retried on its own.</p>
    </section>}
    {error && <p className="draft-error" role="alert">{error}</p>}
    {confirming && <div className="publish-dialog-backdrop">
      <div className="publish-dialog" role="dialog" aria-modal="true"
        aria-labelledby="publish-dialog-title">
        <h4 id="publish-dialog-title">Post this reply to YouTube?</h4>
        <dl>
          <dt>Channel</dt><dd>{state?.eligibility?.channel_title}</dd>
          <dt>Video</dt><dd>{draft.video_id}</dd>
          <dt>Replying to comment</dt><dd>{draft.comment_id}</dd>
          <dt>Saved revision</dt><dd>{draft.revision}</dd>
        </dl>
        <p className="publish-warning">
          This posts a public reply under your channel’s name. It appears on YouTube
          immediately and cannot be un-posted by ReplyAI.
        </p>
        <blockquote className="publish-text">{draft.text}</blockquote>
        {state?.eligibility?.review?.confidence !== null
          && state?.eligibility?.review?.confidence !== undefined
          && <p className="refinement-reason" role="status">
            Secondary-review confidence: {Math.round(state.eligibility.review.confidence * 100)}%.
          </p>}
        {(state?.eligibility?.review?.policy?.length ?? 0) > 0 &&
          <div className="refinement-reason" role="status" aria-label="Reply policy warnings">
            <p>Reply-safety warnings (automatic posting would hold this reply):</p>
            <ul>{state?.eligibility?.review?.policy?.map((v, i) =>
              <li key={i}>{v.rule} · {v.level.replaceAll('_', ' ')} · {v.detail}</li>)}</ul>
          </div>}
        {state?.eligibility?.review?.agreement === 'disagree' &&
          <p className="refinement-reason" role="status">
            The two models classified the original comment differently. Automatic publishing
            is blocked; review the reply before any manual confirmation.
          </p>}
        <div className="draft-actions">
          <button type="button" ref={cancel} disabled={busy}
            onClick={() => {
              setConfirming(false); setError(null); trigger.current?.focus()
            }}>Cancel</button>
          <button type="button" disabled={busy} onClick={() => void confirm()}>Confirm &amp; post</button>
        </div>
        {busy && <p role="status">Posting to YouTube…</p>}
        {error && <p className="draft-error" role="alert">{error}</p>}
      </div>
    </div>}
  </>
}

export default function DraftPanel({ videoId, commentId, csrf }: {
  videoId: string; commentId: string; csrf: string
}) {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [usage, setUsage] = useState<UsageHistory | null>(null)
  const [publication, setPublication] = useState<{ id: string; state: PublicationState } | null>(null)
  const [text, setText] = useState('')
  const [setup, setSetup] = useState<Setup | null>(null)
  const [busy, setBusy] = useState('Loading saved draft…')
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)
  const lock = useRef(false)
  const mounted = useRef(false)
  const lookup = `?video_id=${encodeURIComponent(videoId)}&comment_id=${encodeURIComponent(commentId)}`

  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    void Promise.all([
      request<Setup>('/setup', { signal: controller.signal }),
      request<Draft | null>(lookup, { signal: controller.signal }),
    ]).then(async ([status, saved]) => {
      if (controller.signal.aborted) return
      setSetup(status); setDraft(saved); setText(saved?.text || ''); setLoaded(true)
      if (saved) {
        const history = await request<UsageHistory>(`/${saved.id}/usage`, { signal: controller.signal })
        if (!controller.signal.aborted) setUsage(history)
      }
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Could not load draft.')
    }).finally(() => { if (!controller.signal.aborted) setBusy('') })
    return () => { mounted.current = false; controller.abort() }
  }, [lookup])

  // Posting state comes from the server, so a refresh, or a reload after a lost
  // response, shows what actually happened instead of guessing. Reading it writes
  // nothing and makes no provider request. The record is tagged with its draft so a
  // previous draft's outcome can never be shown against a newly opened one.
  //
  // Eligibility is a judgment about one saved revision, and it changes as soon as the
  // revision, its review, or the local decision changes. Keying the fetch on the draft
  // id alone left the previous answer in place after "Approve draft locally", so the
  // panel could show "approved" while still explaining that an approval was required.
  // The gating fields are dependencies here so the reason and the button refresh together
  // with the state they describe, including after an edit or an applied suggestion clears
  // an older approval.
  const draftId = draft?.id ?? null
  const draftRevision = draft?.revision ?? null
  const draftReview = draft?.review_status ?? null
  const draftDecision = draft?.human_decision ?? null
  useEffect(() => {
    if (!draftId) return
    let active = true
    request<PublicationState>(`/${draftId}/publication`)
      .then(value => { if (active) setPublication({ id: draftId, state: value }) })
      .catch(() => { if (active) setPublication(null) })
    return () => { active = false }
  }, [draftId, draftRevision, draftReview, draftDecision, reloadToken])

  async function reload() {
    if (lock.current) return
    lock.current = true; setBusy('Loading saved draft…'); setError(null)
    try {
      const [status, saved] = await Promise.all([request<Setup>('/setup'), request<Draft | null>(lookup)])
      if (mounted.current) { setSetup(status); setDraft(saved); setText(saved?.text || ''); setLoaded(true) }
      if (saved) {
        const history = await request<UsageHistory>(`/${saved.id}/usage`)
        if (mounted.current) setUsage(history)
      }
      // Remount the refinement panel so it drops any stuck request state and re-reads the
      // persisted conversation. This costs no model call, so a send whose response was
      // lost is recovered here instead of being paid for twice.
      if (mounted.current) setReloadToken(token => token + 1)
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : 'Could not reload draft.')
    } finally { lock.current = false; if (mounted.current) setBusy('') }
  }

  async function act(action: 'generate' | 'edit' | 'regenerate' | 'review' | 'approved' | 'rejected') {
    if (lock.current || busy || !loaded) return
    lock.current = true; setBusy(action === 'generate' || action === 'regenerate' ? 'Generating and reviewing…' : action === 'review' ? 'Reviewing saved reply…' : 'Saving…'); setError(null)
    const suffix = action === 'approved' || action === 'rejected' ? 'decision' : action
    const modelAction = action === 'generate' || action === 'regenerate' || action === 'review'
    const body = action === 'generate' ? { video_id: videoId, comment_id: commentId }
      : { revision: draft?.revision, ...(action === 'edit' ? { text } : {}),
        ...(suffix === 'decision' ? { decision: action } : {}) }
    try {
      const saved = await request<Draft>(action === 'generate' ? '' : `/${draft!.id}/${suffix}`, {
        method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify(body),
        ...(modelAction ? { deadlineMs: MODEL_DEADLINE_MS } : {}),
      })
      if (mounted.current) { setDraft(saved); setText(saved.text) }
      if (action !== 'approved' && action !== 'rejected') {
        const history = await request<UsageHistory>(`/${saved.id}/usage`)
        if (mounted.current) setUsage(history)
      }
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : 'Draft operation failed.')
    } finally { lock.current = false; if (mounted.current) setBusy('') }
  }

  const dirty = draft !== null && text !== draft.text
  const blocked = Boolean(busy) || !loaded
  const aiBlocked = blocked || !setup?.configured
  const isPosted = publication?.id === draft?.id && publication?.state?.publication?.status === 'posted'

  return <section className="flex min-w-0 flex-col gap-5" aria-label="Local reply draft">
    {setup && !setup.configured && <div className="p-3 rounded-lg bg-warning/10 border border-warning/30 text-warning text-sm" role="status">
      AI setup required. Configure {([...setup.missing, ...setup.invalid]).join(', ')} in the backend, restart it, then reload.
    </div>}
    {error && <div className="p-3 rounded-lg bg-error/10 border border-error/30 text-error text-sm" role="alert">{error}</div>}
    
    <div className="flex items-center justify-between">
      <h3 className="text-xl font-bold tracking-tight text-text-main">Reply Workspace</h3>
      <div className="flex items-center gap-2">
        {busy && <span className="text-xs text-brand animate-pulse">{busy}</span>}
        <button type="button" className="px-3 py-1.5 bg-dark-800 border border-dark-600 rounded-lg text-xs font-medium hover:bg-dark-700 transition-colors" disabled={Boolean(busy)} onClick={() => void reload()}>
          Reload
        </button>
      </div>
    </div>

    {!draft ? (
      <div className="py-8 text-center flex flex-col items-center justify-center border border-dashed border-dark-600 rounded-xl bg-dark-900/30">
        <p className="text-text-muted text-sm mb-4">No draft generated yet.</p>
        <button type="button" className="px-4 py-2 bg-brand hover:bg-brand-light text-white rounded-lg text-sm font-medium transition-colors" disabled={aiBlocked} onClick={() => void act('generate')}>
          Generate Draft
        </button>
      </div>
    ) : (
      <>
        {isPosted ? (
          <div className="p-5 rounded-xl bg-success/10 border border-success/30 flex flex-col gap-3 mb-2 shadow-[0_0_20px_rgba(34,197,94,0.1)]">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-success"></span>
              <span className="font-semibold text-success">Posted to YouTube</span>
            </div>
            <p className="text-sm text-text-main">{draft.text}</p>
            {publication?.state?.publication?.reply_comment_id && (
              <div className="text-xs text-text-muted mt-2 border-t border-success/20 pt-3 flex items-center justify-between">
                <div>YouTube ID: <span className="font-mono bg-success/20 px-1.5 py-0.5 rounded text-success ml-1">{publication.state.publication.reply_comment_id}</span></div>
                <a href={`https://youtube.com/comments?lc=${publication.state.publication.reply_comment_id}`} target="_blank" rel="noreferrer" className="text-success hover:underline">View on YouTube</a>
              </div>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-text-main">Reply text</span>
                {dirty && <span className="text-xs text-warning">Unsaved edits will clear approval</span>}
              </div>
              <textarea 
                value={text} 
                maxLength={5000} 
                rows={5} 
                className="box-border w-full min-w-0 max-w-full bg-dark-900 border border-dark-600 rounded-xl p-4 text-sm text-text-main placeholder:text-text-dim focus:border-brand focus:ring-1 focus:ring-brand outline-none transition-all resize-y min-h-[120px]"
                disabled={blocked} 
                onChange={event => setText(event.target.value)} 
                aria-label="reply text"
              />
            </label>
            
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className="px-4 py-2 bg-dark-800 border border-dark-600 hover:bg-dark-700 text-text-main rounded-lg text-sm font-medium transition-colors disabled:opacity-50" disabled={blocked || !dirty || !text.trim()} onClick={() => void act('edit')}>
                Save changes
              </button>
              <button type="button" className="px-4 py-2 bg-dark-800 border border-dark-600 hover:bg-dark-700 text-text-main rounded-lg text-sm font-medium transition-colors disabled:opacity-50" disabled={aiBlocked || dirty} onClick={() => void act('regenerate')}>
                Regenerate
              </button>
              <button type="button" className="px-4 py-2 bg-brand/20 border border-brand/30 hover:bg-brand/30 text-brand-light rounded-lg text-sm font-medium transition-colors disabled:opacity-50" disabled={aiBlocked || dirty || !draft.text || !draft.primary_classification} onClick={() => void act('review')}>
                Review edited
              </button>
            </div>
            
            <div className="min-w-0">
              <RefinementConversation
                key={`${draft.id}:${reloadToken}`}
                draft={draft}
                csrf={csrf}
                dirty={dirty}
                configured={Boolean(setup?.configured)}
                blocked={blocked}
                onApplied={saved => { setDraft(saved); setText(saved.text) }}
              />
            </div>

            <div className="flex flex-col gap-3 p-4 border border-dark-600 rounded-xl bg-dark-800/50">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">Approval Status</span>
                <span className={`text-xs px-2 py-1 rounded-full font-medium ${
                  draft.human_decision === 'approved' ? 'bg-success/20 text-success' : 
                  draft.human_decision === 'rejected' ? 'bg-error/20 text-error' : 
                  'bg-warning/20 text-warning'
                }`}>
                  {draft.human_decision === 'approved' || draft.human_decision === 'rejected'
                    ? `${draft.human_decision} · Rev ${draft.revision}`
                    : 'Pending'}
                </span>
              </div>
              {!dirty && draft.quality && <div className="border-t border-dark-600/50 pt-3" role="status"
                aria-label={`Quality review for saved revision ${draft.revision}`}>
                <p className="text-sm font-medium mb-2">Quality review · saved revision {draft.revision}</p>
                <ul className="space-y-1 text-sm">
                  {qualityCheckLabels.map(([key, label]) => {
                    const result = draft.quality?.[key]
                    const passed = result?.toLowerCase() === 'pass'
                    return <li key={key} className={passed ? 'text-success' : 'text-warning'}>
                      {label}: {result || 'not available'}
                    </li>
                  })}
                </ul>
                {draft.review_status === 'complete'
                  && qualityCheckLabels.some(([key]) => draft.quality?.[key]?.toLowerCase() !== 'pass')
                  && <p className="mt-2 text-sm text-warning">
                    Posting is blocked by the check(s) above. Edit and save the reply, then use
                    “Review edited”; the new review must pass before it can be posted.
                  </p>}
                {draft.quality.findings.length > 0
                  && <p className="mt-2 text-sm text-text-muted">
                    The reviewer recorded {draft.quality.findings.length} finding(s); open technical
                    details to read them.
                  </p>}
              </div>}
              <div className="flex items-center gap-2 pt-2 border-t border-dark-600/50">
                <button type="button" className="flex-1 px-4 py-2 bg-success/20 hover:bg-success/30 text-success border border-success/30 rounded-lg text-sm font-medium transition-colors disabled:opacity-50" disabled={blocked || dirty || draft.review_status !== 'complete'} onClick={() => void act('approved')}>
                  Approve locally
                </button>
                <button type="button" className="flex-1 px-4 py-2 bg-error/20 hover:bg-error/30 text-error border border-error/30 rounded-lg text-sm font-medium transition-colors disabled:opacity-50" disabled={blocked || dirty} onClick={() => void act('rejected')}>
                  Reject locally
                </button>
              </div>
            </div>

            <PublishReply
              draft={draft}
              csrf={csrf}
              state={publication?.id === draft.id && publication.state.eligibility?.revision === draft.revision ? publication.state : null}
              dirty={dirty}
              blocked={blocked}
              onPublished={state => setPublication({ id: draft.id, state })}
            />
          </div>
        )}

        <details className="min-w-0 p-4 border border-dark-600 rounded-xl bg-dark-900/30 group">
          <summary className="text-sm font-medium text-text-muted cursor-pointer outline-none group-hover:text-text-main transition-colors">
            View technical details
          </summary>
          <div className="mt-4 space-y-4 text-sm text-text-dim">
            <div>
              <strong className="block text-text-main mb-1">Original comment snapshot</strong>
              <p className="bg-dark-950 p-2 rounded border border-dark-700">{draft.original_comment}</p>
            </div>
            
            <div className="grid grid-cols-2 gap-4">
              <div>
                <strong className="block text-text-main mb-1">Primary ({draft.providers.primary?.model || 'unconfigured'})</strong>
                <p>{draft.primary_classification ? `${draft.primary_classification.sentiment} · ${draft.primary_classification.intent}` : 'Unavailable'}</p>
              </div>
              <div>
                <strong className="block text-text-main mb-1">Secondary ({draft.providers.secondary?.model || 'unconfigured'})</strong>
                <p>{draft.secondary_classification ? `${draft.secondary_classification.sentiment} · ${draft.secondary_classification.intent}` : 'Unavailable'}</p>
              </div>
            </div>
            
            <div>
              <strong className="block text-text-main mb-1">Agreement:</strong> {draft.agreement}
            </div>

            {draft.quality && !dirty && <div className="border-t border-dark-700 pt-3">
              <strong className="block text-text-main mb-1">Quality findings (Rev {draft.revision})</strong>
              <p className="mb-2">Relevance: {draft.quality.relevance} · Claims: {draft.quality.unsupported_claims} · Inappropriate: {draft.quality.inappropriate_content}</p>
              {draft.quality.confidence !== undefined
                && <p className="mb-2">Secondary-review confidence: {Math.round(draft.quality.confidence * 100)}%.</p>}
              <ul className="list-disc pl-5 space-y-1">{draft.quality.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>
            </div>}
            
            {stageFailures(draft.failure).map(([stage, failure]) => <p className="text-error" key={stage}>
              {stageLabels[stage] || stage}: {failureMessages[failure] || failure}
            </p>)}
            
            <div className="border-t border-dark-700 pt-3">
              <CurrentStageUsage usage={draft.token_usage} />
              <UsageLedger history={usage} />
            </div>
            
            <div className="border-t border-dark-700 pt-3 text-xs">
              {draft.triage && <p>Comment assessment: {draft.triage.category.replaceAll('_', ' ')}.
                {!draft.text && ' No reply was generated for this comment.'}</p>}
              <p>Revision {draft.revision} · Gen: {draft.generation_status} · Review: {draft.review_status}</p>
              {draft.review_status === 'pending' && !busy && <p className="text-warning mt-1">Review pending. Reload to check.</p>}
            </div>
          </div>
        </details>
      </>
    )}
  </section>
}
