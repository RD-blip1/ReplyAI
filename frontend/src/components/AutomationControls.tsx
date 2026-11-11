import { useEffect, useState } from 'react'
import { formatDateTime } from '../lib/format'
import { ChevronRight } from 'lucide-react'
import { apiRequest, messageOf } from '../lib/api'
import type { AutomationSettings } from '../lib/types'

type Allowlist = {
  domains: string[]; url_prefixes: string[]; contacts: string[]; handles: string[]
  approved_statements: string[]; revision: number
}

const lines = (value: string) => value.split(/[\n,]/).map(item => item.trim()).filter(Boolean)

// Owner switches for automatic discovery. Publication itself is governed by the
// publishing mode; these switches decide whether new comments are found and processed.
export default function AutomationControls({ csrf, connected }: { csrf: string; connected: boolean }) {
  const [settings, setSettings] = useState<AutomationSettings | null>(null)
  const [lookback, setLookback] = useState(0)
  const [allowlist, setAllowlist] = useState<Allowlist | null>(null)
  const [domains, setDomains] = useState('')
  const [prefixes, setPrefixes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  useEffect(() => {
    if (!connected) return
    let active = true
    Promise.all([
      apiRequest<AutomationSettings>('/api/automation'),
      apiRequest<Allowlist>('/api/automation/allowlist'),
    ]).then(([loaded, list]) => {
      if (!active) return
      setSettings(loaded)
      setLookback(loaded.lookback_hours)
      setAllowlist(list)
      setDomains(list.domains.join('\n'))
      setPrefixes(list.url_prefixes.join('\n'))
    }).catch(cause => active && setError(messageOf(cause, 'Automation settings could not be loaded.')))
    return () => { active = false }
  }, [connected])

  async function update(next: { enabled: boolean; paused: boolean }) {
    if (next.enabled && !settings?.enabled) {
      const warning = lookback > 0
        ? `ReplyAI will look for new comments every few minutes, including comments from the last ${lookback} hour(s). Continue?`
        : 'ReplyAI will look for new comments every few minutes and process them with your publishing mode. Only comments posted from now on are picked up. Continue?'
      if (!window.confirm(warning)) return
    }
    setBusy(true); setError(null); setSaved(null)
    try {
      const result = await apiRequest<AutomationSettings>('/api/automation', {
        method: 'PUT', headers: { 'X-CSRF-Token': csrf },
        body: JSON.stringify({ ...next, lookback_hours: lookback }),
      })
      setSettings(result)
      setSaved(!result.enabled ? 'Automation turned off.' : result.paused ? 'Automation paused.' : 'Automation is running.')
    } catch (cause) {
      setError(messageOf(cause, 'Automation settings could not be saved.'))
    } finally {
      setBusy(false)
    }
  }

  async function saveAllowlist() {
    if (!allowlist) return
    setBusy(true); setError(null); setSaved(null)
    try {
      const result = await apiRequest<Allowlist>('/api/automation/allowlist', {
        method: 'PUT', headers: { 'X-CSRF-Token': csrf },
        body: JSON.stringify({ ...allowlist, domains: lines(domains), url_prefixes: lines(prefixes) }),
      })
      setAllowlist(result)
      setSaved('Allowed links saved.')
    } catch (cause) {
      setError(messageOf(cause, 'Allowed links could not be saved.'))
    } finally {
      setBusy(false)
    }
  }

  if (!connected) {
    return <p className="text-sm text-text-muted">Connect a YouTube channel to turn on automatic operation.</p>
  }
  if (!settings) {
    return error ? <p role="alert" className="text-sm text-danger">{error}</p>
      : <p role="status" className="text-sm text-text-muted">Loading automation settings…</p>
  }
  const state = !settings.enabled ? 'Off' : settings.paused ? 'Paused' : 'Running'
  return (
    <div className="space-y-4" aria-label="Automatic operation">
      <p role="status" className="flex flex-wrap items-center gap-x-2 text-[0.9375rem] text-text-muted">
        Status: <span className="inline-flex items-center gap-2 font-semibold text-text-main">
          <span className={`h-2 w-2 rounded-full ${state === 'Running' ? 'bg-brand' : state === 'Paused' ? 'bg-warning' : 'bg-text-muted'}`} aria-hidden="true" />{state}</span>
        {settings.last_poll_finished_at ? ` · last checked ${formatDateTime(settings.last_poll_finished_at)}` : ''}
      </p>
      <label className="block text-sm">
        <span className="block text-text-dim mb-2">Also process comments from the last (hours, 0–72)</span>
        <input type="number" min={0} max={72} value={lookback} disabled={busy || settings.enabled}
          onChange={event => setLookback(Math.max(0, Math.min(72, Number(event.target.value) || 0)))}
          className="input w-28" aria-label="Lookback hours" />
        {lookback > 0 && !settings.enabled ? <span className="ml-2 text-warning text-xs">Older comments will be processed too.</span> : null}
      </label>
      <div className="flex flex-wrap gap-2">
        {!settings.enabled
          ? <button type="button" disabled={busy} onClick={() => void update({ enabled: true, paused: false })}
              className="btn btn-primary">Turn on automation</button>
          : <>
              <button type="button" disabled={busy} onClick={() => void update({ enabled: true, paused: !settings.paused })}
                className="btn">
                {settings.paused ? 'Resume' : 'Pause'}</button>
              <button type="button" disabled={busy} onClick={() => void update({ enabled: false, paused: false })}
                className="btn btn-danger">Turn off</button>
            </>}
      </div>
      <details className="disclosure text-sm">
        <summary className="text-text-dim"><ChevronRight className="disclosure-chevron" aria-hidden="true" />Allowed links in replies</summary>
        <p className="mt-2 text-xs text-text-muted">Automatic replies containing any other link are held for you. One per line.</p>
        <label className="block mt-2">Domains
          <textarea aria-label="Allowed domains" value={domains} onChange={e => setDomains(e.target.value)}
            className="textarea mt-1" rows={2} /></label>
        <label className="block mt-2">Link prefixes
          <textarea aria-label="Allowed link prefixes" value={prefixes} onChange={e => setPrefixes(e.target.value)}
            className="textarea mt-1" rows={2} /></label>
        <button type="button" disabled={busy} onClick={() => void saveAllowlist()}
          className="btn btn-sm mt-3">Save allowed links</button>
      </details>
      {saved ? <p role="status" className="text-sm text-brand">{saved}</p> : null}
      {error ? <p role="alert" className="text-sm text-danger">{error}</p> : null}
    </div>
  )
}
