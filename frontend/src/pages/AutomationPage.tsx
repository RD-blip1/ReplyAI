import { useCallback, useEffect, useState } from 'react'
import { formatDateTime } from '../lib/format'
import { ChevronRight, CircleCheck, CircleX, ClipboardCheck, Eye, RefreshCw, RotateCcw, Shield, Zap } from 'lucide-react'
import { KpiBody, PageHeader, type Tone } from '../components/ui'
import { apiRequest, messageOf } from '../lib/api'
import AutomationControls from '../components/AutomationControls'
import { CLASS_LABELS, decisionBadge, explain, isDryRun } from '../lib/decisions'
import type { AutomationDecision, AutomationSettings, AutomationSummary, DecisionClass } from '../lib/types'
import type { useAppData } from '../hooks/useAppData'

const CLASSES: DecisionClass[] = ['AUTO_PUBLISH', 'HUMAN_REVIEW', 'SKIP', 'POLICY_BLOCK', 'RETRY', 'TECHNICAL_FAILURE']
const CLASS_ICONS: Record<DecisionClass, { icon: typeof Eye; tone: Tone }> = {
  AUTO_PUBLISH: { icon: CircleCheck, tone: 'green' },
  HUMAN_REVIEW: { icon: ClipboardCheck, tone: 'amber' },
  SKIP: { icon: Eye, tone: 'neutral' },
  POLICY_BLOCK: { icon: Shield, tone: 'red' },
  RETRY: { icon: RotateCcw, tone: 'amber' },
  TECHNICAL_FAILURE: { icon: CircleX, tone: 'neutral' },
}

function Badge({ value, result }: { value: DecisionClass; result?: string | null }) {
  const badge = decisionBadge(value, result)
  return <span className={badge.style}>
    {badge.label}</span>
}

function when(value: string | null | undefined) {
  return formatDateTime(value) ?? '—'
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
  return <section aria-label="Decision detail" className="well mt-4 p-4 text-sm sm:p-5">
    <div className="mb-2 flex flex-wrap items-center gap-2"><Badge value={decision.decision_class} result={decision.execution_result} />
      <code className="text-xs text-text-dim">{decision.decision_code}</code>
      <span className="text-xs text-text-muted">{decision.execution_result}</span></div>
    <p className="mb-3 text-[0.9375rem]">{explain(decision.decision_code)}</p>
    <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[10rem_minmax(0,1fr)] [overflow-wrap:anywhere]">
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
  const dryRuns = (summary?.decisions ?? []).filter(row => row.decision_class === 'AUTO_PUBLISH' && isDryRun(row.execution_result))
    .reduce((total, row) => total + row.count, 0)
  const state = !settings?.enabled ? 'Off' : settings.paused ? 'Paused' : 'Running'

  return <div className="page">
    <PageHeader
      title="Automation"
      subtitle="Let ReplyAI discover new comments and act inside your rules."
      actions={<>
        <button type="button" onClick={() => void pollNow()} disabled={!settings?.enabled || settings?.paused}
          className="btn btn-primary btn-lg"
          title={!settings?.enabled ? 'Turn automation on to check for new comments' : settings?.paused ? 'Resume automation to check for new comments' : undefined}>
          <Zap aria-hidden="true" />Poll now</button>
        <button type="button" onClick={() => void load()} aria-label="Refresh"
          className="btn btn-lg btn-icon w-11"><RefreshCw className={loading ? 'animate-spin' : ''} aria-hidden="true" /></button>
      </>}
    />
    {error ? <p role="alert" className="alert alert-error">{error}</p> : null}
    {notice ? <p role="status" className="alert">{notice}</p> : null}
    <section aria-label="Discovery status" className="card automation-status">
      <div>
        <span className="automation-status-label">Status</span>
        <span className="automation-status-value inline-flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${state === 'Running' ? 'bg-brand' : state === 'Paused' ? 'bg-warning' : 'bg-text-muted'}`} aria-hidden="true" />
          {state}</span>
      </div>
      <div><span className="automation-status-label">Last check</span><span className="automation-status-value">{when(settings?.last_poll_finished_at)}</span></div>
      <div><span className="automation-status-label">Next check</span><span className="automation-status-value">{when(settings?.next_poll_at)}</span></div>
      <div><span className="automation-status-label">Last error</span><span className="automation-status-value [overflow-wrap:anywhere]">{String(settings?.last_error?.code ?? '—')}</span></div>
    </section>
    <details className="card disclosure">
      <summary className="px-5 py-4 text-lg font-semibold text-text-main sm:px-6"><ChevronRight className="disclosure-chevron" aria-hidden="true" />Controls</summary>
      <div className="border-t border-[var(--line)] px-5 py-5 sm:px-6"><AutomationControls csrf={csrf} connected /></div>
    </details>
    <section aria-label="Last 24 hours" className="kpi-grid automation-kpis">
      {CLASSES.map(c => <button type="button" key={c} onClick={() => setFilter(filter === c ? '' : c)}
        aria-pressed={filter === c}
        className="kpi">
        <KpiBody icon={CLASS_ICONS[c].icon} tone={CLASS_ICONS[c].tone} value={counts[c]} testId={`count-${c}`}
          label={c === 'AUTO_PUBLISH' && dryRuns > 0 ? `Auto-publish eligible (${dryRuns} dry run)` : CLASS_LABELS[c]} />
      </button>)}
    </section>
    <section aria-label="Recent decisions" className="card card-pad">
      <h2 className="card-title mb-3">Recent decisions{filter ? ` · ${CLASS_LABELS[filter]}` : ''}</h2>
      {loading ? <p role="status" className="text-sm text-text-muted">Loading…</p> : null}
      {!loading && items.length === 0 ? <p className="text-[0.9375rem] text-text-muted">No automatic decisions yet.</p> : null}
      <ul className="divide-y divide-[var(--line)] border-t border-[var(--line)]">
        {items.map(item => <li key={item.id}>
          <button type="button" onClick={() => setSelected(item)} aria-pressed={selected?.id === item.id}
            className={`automation-decision ${selected?.id === item.id ? 'is-selected' : ''}`}>
            <span className="automation-decision-badge"><Badge value={item.decision_class} result={item.execution_result} /></span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-base text-text-main">{String(item.trace?.comment ?? item.comment_id)}</span>
              <span className="mt-0.5 block text-sm text-text-muted">{explain(item.decision_code)}</span>
            </span>
            <span className="shrink-0 text-sm text-text-muted tabular-nums">{when(item.created_at)}</span>
          </button>
        </li>)}
      </ul>
      {selected ? <Trace decision={selected} /> : null}
    </section>
  </div>
}
