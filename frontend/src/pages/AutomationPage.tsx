import { useCallback, useEffect, useState } from 'react'
import { Bot, RefreshCw } from 'lucide-react'
import { apiRequest, messageOf } from '../lib/api'
import AutomationControls from '../components/AutomationControls'
import { CLASS_LABELS, CLASS_STYLES, explain } from '../lib/decisions'
import type { AutomationDecision, AutomationSettings, AutomationSummary, DecisionClass } from '../lib/types'
import type { useAppData } from '../hooks/useAppData'

const CLASSES: DecisionClass[] = ['AUTO_PUBLISH', 'HUMAN_REVIEW', 'SKIP', 'POLICY_BLOCK', 'RETRY', 'TECHNICAL_FAILURE']

function Badge({ value }: { value: DecisionClass }) {
  return <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-semibold ${CLASS_STYLES[value]}`}>
    {CLASS_LABELS[value]}</span>
}

function when(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : '—'
}

function Trace({ decision }: { decision: AutomationDecision }) {
  const trace = decision.trace as Record<string, unknown>
  const providers = (trace.providers ?? {}) as Record<string, { model?: string }>
  const rows: [string, unknown][] = [
    ['Comment', trace.comment], ['Reply', trace.reply],
    ['Triage', trace.triage], ['Generator (primary)', providers.primary?.model],
    ['Verifier (secondary)', providers.secondary?.model],
    ['Primary labels', trace.primary_classification], ['Secondary labels', trace.secondary_classification],
    ['Agreement', trace.agreement], ['Quality review', trace.quality], ['Reply policy', trace.policy],
    ['Input signals', (trace.input_signals as { high_risk_kinds?: string[] } | undefined)?.high_risk_kinds],
    ['Stage failures', trace.stage_failures], ['Publication', trace.publication],
  ]
  return <section aria-label="Decision detail" className="mt-4 rounded-xl border border-dark-600 bg-dark-900/60 p-4 text-sm">
    <div className="flex items-center gap-2 mb-2"><Badge value={decision.decision_class} />
      <code className="text-xs">{decision.decision_code}</code>
      <span className="text-text-muted text-xs">{decision.execution_result}</span></div>
    <p className="mb-3">{explain(decision.decision_code)}</p>
    <dl className="grid grid-cols-[10rem_1fr] gap-x-3 gap-y-1">
      {rows.filter(([, value]) => value !== undefined && value !== null).map(([label, value]) => <div key={label} className="contents">
        <dt className="text-text-muted">{label}</dt>
        <dd className="break-words">{typeof value === 'string' ? value : <code className="text-xs">{JSON.stringify(value)}</code>}</dd>
      </div>)}
    </dl>
    <p className="mt-2 text-xs text-text-muted">Policy {decision.policy_version} · {when(decision.created_at)}</p>
  </section>
}

export default function AutomationPage({ appData }: { appData: ReturnType<typeof useAppData> }) {
  const { session } = appData
  const csrf = session?.csrf_token ?? ''
  const [settings, setSettings] = useState<AutomationSettings | null>(null)
  const [summary, setSummary] = useState<AutomationSummary | null>(null)
  const [items, setItems] = useState<AutomationDecision[]>([])
  const [selected, setSelected] = useState<AutomationDecision | null>(null)
  const [filter, setFilter] = useState<DecisionClass | ''>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!session?.connected) return
    setLoading(true)
    setError(null)
    try {
      const query = filter ? `?class=${filter}` : ''
      const [s, sum, activity] = await Promise.all([
        apiRequest<AutomationSettings>('/api/automation'),
        apiRequest<AutomationSummary>('/api/automation/summary'),
        apiRequest<{ items: AutomationDecision[] }>(`/api/automation/activity${query}`),
      ])
      setSettings(s); setSummary(sum); setItems(activity.items)
    } catch (cause) {
      setError(messageOf(cause, 'Automation activity could not be loaded.'))
    } finally {
      setLoading(false)
    }
  }, [session?.connected, filter])

  useEffect(() => { void load() }, [load])

  async function pollNow() {
    setNotice(null)
    try {
      await apiRequest('/api/automation/poll', { method: 'POST', headers: { 'X-CSRF-Token': csrf } })
      setNotice('A check for new comments was scheduled.')
    } catch (cause) {
      setNotice(messageOf(cause, 'Could not schedule a check.'))
    }
  }

  if (!session?.connected) {
    return <p className="text-text-muted">Connect a YouTube channel to see automation activity.</p>
  }
  const counts = Object.fromEntries(CLASSES.map(c => [c, 0])) as Record<DecisionClass, number>
  for (const row of summary?.decisions ?? []) counts[row.decision_class] = (counts[row.decision_class] ?? 0) + row.count
  const state = !settings?.enabled ? 'Off' : settings.paused ? 'Paused' : 'Running'

  return <div className="space-y-6">
    <div className="flex items-center justify-between gap-3 flex-wrap">
      <h1 className="text-2xl font-bold flex items-center gap-2"><Bot className="w-6 h-6 text-brand" /> Automation</h1>
      <div className="flex gap-2">
        <button type="button" onClick={() => void pollNow()} disabled={!settings?.enabled || settings?.paused}
          className="px-3 py-2 rounded-lg border border-dark-600 text-sm disabled:opacity-50">Poll now</button>
        <button type="button" onClick={() => void load()} aria-label="Refresh"
          className="px-3 py-2 rounded-lg border border-dark-600 text-sm"><RefreshCw className="w-4 h-4" /></button>
      </div>
    </div>
    {error ? <p role="alert" className="text-danger text-sm">{error}</p> : null}
    {notice ? <p role="status" className="text-sm">{notice}</p> : null}
    <section aria-label="Discovery status" className="rounded-2xl border border-dark-600 bg-dark-800/50 p-4 text-sm grid gap-1 sm:grid-cols-4">
      <div><span className="text-text-muted block">Status</span><span className="font-semibold">{state}</span></div>
      <div><span className="text-text-muted block">Last check</span>{when(settings?.last_poll_finished_at)}</div>
      <div><span className="text-text-muted block">Next check</span>{when(settings?.next_poll_at)}</div>
      <div><span className="text-text-muted block">Last error</span>{String(settings?.last_error?.code ?? '—')}</div>
    </section>
    <details className="rounded-2xl border border-dark-600 bg-dark-800/50 p-4">
      <summary className="cursor-pointer text-sm font-semibold">Controls</summary>
      <div className="mt-3"><AutomationControls csrf={csrf} connected /></div>
    </details>
    <section aria-label="Last 24 hours" className="grid grid-cols-2 sm:grid-cols-6 gap-3">
      {CLASSES.map(c => <button type="button" key={c} onClick={() => setFilter(filter === c ? '' : c)}
        aria-pressed={filter === c}
        className={`rounded-xl border p-3 text-left ${filter === c ? 'border-brand' : 'border-dark-600'} bg-dark-800/50`}>
        <span className="block text-2xl font-bold" data-testid={`count-${c}`}>{counts[c]}</span>
        <span className="text-xs text-text-muted">{CLASS_LABELS[c]}</span>
      </button>)}
    </section>
    <section aria-label="Recent decisions" className="rounded-2xl border border-dark-600 bg-dark-800/50 p-4">
      <h2 className="font-semibold mb-3">Recent decisions{filter ? ` · ${CLASS_LABELS[filter]}` : ''}</h2>
      {loading ? <p role="status" className="text-sm text-text-muted">Loading…</p> : null}
      {!loading && items.length === 0 ? <p className="text-sm text-text-muted">No automatic decisions yet.</p> : null}
      <ul className="divide-y divide-dark-600">
        {items.map(item => <li key={item.id}>
          <button type="button" onClick={() => setSelected(item)} className="w-full text-left py-2 flex gap-3 items-start">
            <Badge value={item.decision_class} />
            <span className="flex-1 min-w-0">
              <span className="block truncate text-sm">{String(item.trace?.comment ?? item.comment_id)}</span>
              <span className="block text-xs text-text-muted">{explain(item.decision_code)}</span>
            </span>
            <span className="text-xs text-text-muted shrink-0">{when(item.created_at)}</span>
          </button>
        </li>)}
      </ul>
      {selected ? <Trace decision={selected} /> : null}
    </section>
  </div>
}
