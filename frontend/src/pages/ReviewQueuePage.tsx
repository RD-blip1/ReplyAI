import { useCallback, useEffect, useState } from 'react'
import { ClipboardCheck } from 'lucide-react'
import DraftPanel from '../DraftPanel'
import { apiRequest, messageOf } from '../lib/api'
import { CLASS_LABELS, CLASS_STYLES, explain } from '../lib/decisions'
import type { ReviewItem } from '../lib/types'
import type { useAppData } from '../hooks/useAppData'

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

  if (!session?.connected) return <p className="text-text-muted">Connect a YouTube channel to review held replies.</p>

  return <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
    <section aria-label="Review queue" className="space-y-3">
      <h1 className="text-2xl font-bold flex items-center gap-2"><ClipboardCheck className="w-6 h-6 text-brand" /> Review queue</h1>
      <div role="radiogroup" aria-label="Filter" className="flex gap-2">
        {(['all', 'HUMAN_REVIEW', 'POLICY_BLOCK'] as Filter[]).map(value => <button key={value} type="button"
          role="radio" aria-checked={filter === value} onClick={() => setFilter(value)}
          className={`px-3 py-1.5 rounded-full border text-sm ${filter === value ? 'border-brand text-brand' : 'border-dark-600'}`}>
          {value === 'all' ? 'All' : CLASS_LABELS[value]}</button>)}
      </div>
      {error ? <p role="alert" className="text-sm text-danger">{error}</p> : null}
      {loading ? <p role="status" className="text-sm text-text-muted">Loading…</p> : null}
      {!loading && !error && items.length === 0 ? <p className="text-sm text-text-muted">Nothing is waiting for you.</p> : null}
      <ul className="space-y-2">
        {items.map(item => <li key={item.decision_id}
          className={`rounded-xl border p-3 ${open?.decision_id === item.decision_id ? 'border-brand' : 'border-dark-600'} bg-dark-800/50`}>
          <div className="flex items-center gap-2 mb-1">
            <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${CLASS_STYLES[item.decision_class]}`}>
              {CLASS_LABELS[item.decision_class]}</span>
            <span className="text-xs text-text-muted">{new Date(item.created_at).toLocaleString()}</span>
          </div>
          <p className="text-sm font-medium">Why: {explain(item.decision_code)}</p>
          <p className="text-sm text-text-muted mt-1 line-clamp-2">“{item.comment}”</p>
          {item.policy.length > 0 ? <ul className="mt-1 text-xs text-danger">
            {item.policy.map((v, i) => <li key={i}>{v.rule}: {v.detail} ({v.level.replaceAll('_', ' ')})</li>)}
          </ul> : null}
          <div className="mt-2 flex gap-2">
            <button type="button" onClick={() => setOpen(item)} className="px-3 py-1 rounded-lg bg-brand text-white text-sm">Open draft</button>
            <button type="button" onClick={() => void dismiss(item)} className="px-3 py-1 rounded-lg border border-dark-600 text-sm">Dismiss</button>
          </div>
        </li>)}
      </ul>
    </section>
    <section aria-label="Selected draft">
      {open ? <>
        <p className="text-sm text-text-muted mb-2">Edit, approve and confirm as usual. Nothing posts without your confirmation.</p>
        <DraftPanel key={open.decision_id} videoId={open.video_id} commentId={open.comment_id} csrf={csrf} />
      </> : <p className="text-sm text-text-muted">Choose an item to open its draft.</p>}
    </section>
  </div>
}
