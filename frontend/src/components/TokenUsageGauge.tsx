// LLM usage gauge. Original design and polling logic by teammate Hrishik1033 (PR #3,
// commit 1a74aab); reproduced by hand and extended with request counts, both roles'
// ceilings and a local replies-remaining estimate.
import { useEffect, useState, type ReactNode } from 'react'
import { apiRequest, messageOf } from '../lib/api'
import type { RoleTokenUsage, UsageEstimate, UsageSummary } from '../lib/types'

const POLL_MS = 10_000
const ROLE_LABELS: Record<'primary' | 'secondary', string> = {
  primary: 'Primary (writes replies)',
  secondary: 'Secondary (checks replies)',
}
const TONES = {
  success: { stroke: 'stroke-brand', text: 'text-brand', bar: 'bg-brand', glow: 'rgba(46,230,166,0.55)' },
  warning: { stroke: 'stroke-warning', text: 'text-warning', bar: 'bg-warning', glow: 'rgba(241,189,95,0.5)' },
  danger: { stroke: 'stroke-danger', text: 'text-danger', bar: 'bg-danger', glow: 'rgba(242,129,122,0.5)' },
}

function toneFor(percent: number) {
  if (percent > 85) return TONES.danger
  if (percent >= 60) return TONES.warning
  return TONES.success
}

const clamp = (value: number) => Math.min(100, Math.max(0, value))
const fmt = (n: number) => n.toLocaleString()

function RoleRow({ role, data }: { role: 'primary' | 'secondary'; data: RoleTokenUsage }) {
  const tone = toneFor(data.percent)
  return <div className="space-y-2" data-testid={`usage-${role}`}>
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
      <span className="text-[0.9375rem] font-semibold text-text-main">{ROLE_LABELS[role]}</span>
      <span className="text-sm text-text-muted tabular-nums">
        {data.limit > 0
          ? `${fmt(data.used)} / ${fmt(data.limit)} tokens · ${Math.round(data.percent)}%`
          : `${fmt(data.used)} tokens · no ceiling`}
      </span>
    </div>
    <div className="h-2 overflow-hidden rounded-full bg-dark-600">
      <div className={`h-full rounded-full ${tone.bar} transition-[width] duration-700 ease-out`}
        style={{ width: `${clamp(data.percent)}%`, boxShadow: `0 0 12px ${tone.glow}` }} />
    </div>
    <p className="text-sm text-text-muted tabular-nums">
      {fmt(data.requests)}{data.request_limit > 0 ? ` / ${fmt(data.request_limit)}` : ''} requests
      {data.unknown_requests > 0 ? ` · ${fmt(data.unknown_requests)} without token counts` : ''}
    </p>
  </div>
}

function Estimate({ estimate }: { estimate: UsageEstimate }) {
  if (estimate.replies === null) {
    return <div className="well p-4 text-[0.9375rem] text-text-dim" data-testid="usage-estimate">
      {estimate.message}
      {estimate.reason === 'not_enough_history' ? (
        <span className="block text-sm text-text-muted mt-1">{estimate.completed_replies} of {estimate.min_history} completed replies recorded.</span>
      ) : null}
    </div>
  }
  const limiting = estimate.limiting
  return <div className="usage-estimate rounded-2xl border border-brand/25 p-4 sm:p-5" data-testid="usage-estimate">
    <p className="eyebrow">Local estimate · replies remaining</p>
    <p className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-text-main">
      <span className="usage-estimate-value">≈ {fmt(estimate.replies)}</span>
      <span className="text-[0.9375rem] text-text-dim">more replies fit in the rolling 24-hour window</span>
    </p>
    <p className="mt-2 text-sm leading-relaxed text-text-muted">
      {limiting ? `Limited by ${limiting.role} ${limiting.resource}. ` : ''}
      Averaged over the last {estimate.completed_replies} completed replies. {estimate.message}
    </p>
  </div>
}

// 240-degree dial: starts at the lower left, runs over the top, ends at the lower right.
const DIAL = 'M 30.72 140 A 80 80 0 1 1 169.28 140'

export default function TokenUsageGauge({ connected, heading }: { connected: boolean; heading?: ReactNode }) {
  const [data, setData] = useState<UsageSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!connected) return
    let disposed = false
    let sequence = 0
    let controller: AbortController | null = null

    const load = async () => {
      const current = ++sequence
      controller?.abort()
      const next = new AbortController()
      controller = next
      try {
        const result = await apiRequest<UsageSummary>('/api/usage/summary', { signal: next.signal })
        if (disposed || current !== sequence) return
        setData(result)
        setError(null)
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === 'AbortError') return
        if (disposed || current !== sequence) return
        setError(messageOf(cause, 'Usage could not be loaded.'))
      }
    }

    void load()
    const timer = window.setInterval(() => { void load() }, POLL_MS)
    return () => {
      disposed = true
      sequence += 1
      controller?.abort()
      window.clearInterval(timer)
    }
  }, [connected])

  const header = (label?: ReactNode, badge?: ReactNode) => (heading || label || badge) ? <div className="flex items-start justify-between gap-3">
    <div className="min-w-0">
      {heading}
      {label}
    </div>
    {badge}
  </div> : null

  if (!connected) return <div className="space-y-4">{header()}<p className="text-[0.9375rem] text-text-muted">Connect a YouTube channel to see LLM usage.</p></div>
  if (error && !data) return <div className="space-y-4">{header()}<p role="alert" className="text-[0.9375rem] text-danger">{error}</p></div>
  if (!data) return <div className="space-y-4">{header()}<p role="status" className="text-[0.9375rem] text-text-muted">Loading LLM usage…</p></div>

  const { llm } = data
  const tone = toneFor(llm.percent)
  const percent = clamp(llm.percent)

  return <div className="space-y-5">
    {header(
      <>
        <p className="mt-1 text-xs font-semibold uppercase tracking-[0.14em] text-text-muted">Last {llm.window_hours} hours · rolling</p>
        {error ? <p role="status" className="mt-1 text-sm text-warning">{error}</p> : null}
      </>,
      data.simulation ? <span className="badge badge-amber">Simulated counts</span> : null,
    )}

    <Estimate estimate={data.estimate} />

    <p className="text-center text-sm text-text-muted">Gauge shows the busier role ({llm.headline_role}).</p>

    <div className="grid grid-cols-1 items-center gap-5 sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)] sm:gap-7">
      <div className="mx-auto w-full max-w-[11rem]">
        <svg viewBox="0 0 200 172" role="img" className="block w-full overflow-visible"
          aria-label={`${Math.round(llm.percent)}% of the ${llm.headline_role} model's configured daily token ceiling used`}>
          <path d={DIAL} pathLength={100} fill="none" strokeWidth={16} strokeLinecap="round" className="stroke-dark-600" />
          {/* Only drawn above 0 %: a zero-length dash with a round cap would paint a dot. */}
          {percent > 0 ? (
            <path d={DIAL} pathLength={100} fill="none" strokeWidth={16}
              strokeLinecap="round" strokeDasharray={`${percent} 100`} className={tone.stroke}
              style={{ transition: 'stroke-dasharray 700ms ease-out', filter: `drop-shadow(0 0 6px ${tone.glow})` }}
              data-testid="usage-arc" />
          ) : null}
          <text x={100} y={112} textAnchor="middle" className={`fill-current ${tone.text}`}
            style={{ fontSize: 38, fontWeight: 700, letterSpacing: '-0.02em' }}>
            {Math.round(llm.percent)}%
          </text>
        </svg>
      </div>
      <div className="min-w-0 space-y-4">
        <RoleRow role="primary" data={llm.roles.primary} />
        <RoleRow role="secondary" data={llm.roles.secondary} />
      </div>
    </div>

    <p className="text-[0.8125rem] leading-relaxed text-text-muted">
      Counted from ReplyAI's own records of requests made for this channel. Ceilings are the
      values configured in ReplyAI (<code className="text-text-dim">LLM_*_TOKENS_PER_DAY</code>, <code className="text-text-dim">LLM_REQUESTS_PER_DAY</code>),
      not the provider's live quota: other apps using the same key and the provider's own reset time are not visible here.
      {llm.unknown_requests > 0 ? ' Requests without token counts are not counted as zero.' : ''}
    </p>
  </div>
}
