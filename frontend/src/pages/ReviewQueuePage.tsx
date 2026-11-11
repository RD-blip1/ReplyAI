import { useCallback, useEffect, useState } from 'react'
import { formatDateTime } from '../lib/format'
import { ClipboardCheck, FileText, X } from 'lucide-react'
import { PageHeader } from '../components/ui'
import DraftPanel from '../DraftPanel'
import { apiRequest, messageOf } from '../lib/api'
import { CLASS_LABELS, CLASS_STYLES, explain } from '../lib/decisions'
import type { ReviewItem } from '../lib/types'
import type { useAppData } from '../hooks/useAppData'
import { SafeText } from '../components/SafeText'

type Filter = 'all' | 'HUMAN_REVIEW' | 'POLICY_BLOCK'

// Items the automation could not decide on its own. Resolution reuses the existing draft
// panel (edit → approve → confirm & post), so every manual safeguard still applies.
export default function ReviewQueuePage({ appData }: { appData: ReturnType<typeof useAppData> }) {
  const { session } = appData
  const csrf = session?.csrf_token ?? ''
  const [filter, setFilter] = useState<Filter>('all')
  const [items, setItems] = useState<ReviewItem[]>([])
  const [open, setOpen] = useState<ReviewItem | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!session?.connected) return
    setLoading(true)
    setError(null)
    try {
      const result = await apiRequest<{ items: ReviewItem[] }>(`/api/review-queue?class=${filter}`)
      setItems(result.items)
    } catch (cause) {
      setError(messageOf(cause, 'The review queue could not be loaded.'))
    } finally {
      setLoading(false)
    }
  }, [session?.connected, filter])

  useEffect(() => { void load() }, [load])

  async function dismiss(item: ReviewItem) {
    try {
      await apiRequest(`/api/review-queue/${item.decision_id}/dismiss`, {
        method: 'POST', headers: { 'X-CSRF-Token': csrf } })
      if (open?.decision_id === item.decision_id) setOpen(null)
      setItems(current => current.filter(i => i.decision_id !== item.decision_id))
    } catch (cause) {
      setError(messageOf(cause, 'The item could not be dismissed.'))
    }
  }

  if (!session?.connected) return <div className="page">
    <PageHeader title="Review queue" subtitle="Comments the automation held for you. Nothing posts without your confirmation." />
    <div className="card empty-state"><ClipboardCheck aria-hidden="true" /><p>Connect a YouTube channel to review held replies.</p></div>
  </div>

  // Nothing open: the cards fill the page in a two-column grid. A draft open: the list
  // narrows to one column beside the draft panel.
  return <div className="page">
    <PageHeader
      title="Review queue"
      subtitle="Comments the automation held for you. Nothing posts without your confirmation."
      actions={<div role="radiogroup" aria-label="Filter" className="seg">
        {(['all', 'HUMAN_REVIEW', 'POLICY_BLOCK'] as Filter[]).map(value => <button key={value} type="button"
          role="radio" aria-checked={filter === value} onClick={() => setFilter(value)}
          className="seg-item">
          {value === 'all' ? 'All' : CLASS_LABELS[value]}</button>)}
      </div>}
    />
    <div className={open ? 'grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]' : ''}>
    <section aria-label="Review queue" className="min-w-0 space-y-4">
      {error ? <p role="alert" className="alert alert-error">{error}</p> : null}
      {loading ? <p role="status" className="text-[0.9375rem] text-text-muted">Loading…</p> : null}
      {!loading && !error && items.length === 0 ? <div className="card empty-state">
        <ClipboardCheck aria-hidden="true" /><p>Nothing is waiting for you.</p></div> : null}
      <ul className={`grid gap-4 sm:gap-5 ${open ? 'grid-cols-1' : 'grid-cols-1 lg:grid-cols-2'}`}>
        {items.map(item => <li key={item.decision_id}
          className={`card review-card ${open?.decision_id === item.decision_id ? 'is-open' : ''}`}>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <span className={CLASS_STYLES[item.decision_class]}>{CLASS_LABELS[item.decision_class]}</span>
            <span className="text-sm text-text-muted tabular-nums">{formatDateTime(item.created_at)}</span>
          </div>
          <p className="text-[1.0625rem] font-bold leading-snug text-text-main">Why: {explain(item.decision_code)}</p>
          <p className="well mt-3 px-4 py-3 text-[0.9375rem] text-text-dim line-clamp-3 break-words [overflow-wrap:anywhere]">“<SafeText text={item.comment} />”</p>
          {item.policy.length > 0 ? <ul className="mt-3 space-y-1 font-mono text-[0.8125rem] text-danger break-words [overflow-wrap:anywhere]">
            {item.policy.map((v, i) => <li key={i}>{v.rule}: {v.detail} ({v.level.replaceAll('_', ' ')})</li>)}
          </ul> : null}
          <div className="mt-auto flex flex-wrap gap-2 pt-4">
            <button type="button" onClick={() => setOpen(item)} className="btn btn-primary btn-lg">
              <FileText aria-hidden="true" />Open draft</button>
            <button type="button" onClick={() => void dismiss(item)} className="btn btn-lg">Dismiss</button>
          </div>
        </li>)}
      </ul>
    </section>
    {open ? <section aria-label="Selected draft" className="card card-pad min-w-0 xl:sticky xl:top-4">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 text-[0.9375rem] text-text-muted">Edit, approve and confirm as usual. Nothing posts without your confirmation.</p>
        <button type="button" onClick={() => setOpen(null)} className="btn">
          <X aria-hidden="true" />Close draft</button>
      </div>
      <DraftPanel key={open.decision_id} videoId={open.video_id} commentId={open.comment_id} csrf={csrf}
        simulated={Boolean(session?.simulation)} />
    </section> : null}
    </div>
  </div>
}
