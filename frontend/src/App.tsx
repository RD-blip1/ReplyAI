import { useEffect, useState } from 'react'
import {
  Activity,
  ArrowUpRight,
  Check,
  CircleHelp,
  Clock3,
  Database,
  MessageCircle,
  Radio,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Video,
} from 'lucide-react'

type ServiceStatus = 'checking' | 'available' | 'unavailable'

type ReadinessReport = {
  status: 'ready' | 'not_ready'
  checks: {
    database: 'available' | 'unavailable'
    redis: 'available' | 'unavailable'
  }
}

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8001').replace(/\/$/, '')

const capabilities = [
  { icon: Video, name: 'YouTube connection', detail: 'OAuth authorization', status: 'Not available' },
  { icon: MessageCircle, name: 'Comment inbox', detail: 'Channel comment sync', status: 'Not available' },
  { icon: Sparkles, name: 'AI reply assistant', detail: 'Verified reply suggestions', status: 'Not available' },
  { icon: ShieldCheck, name: 'Review & publishing', detail: 'Human approval controls', status: 'Not available' },
]

function StatusLabel({ status }: { status: ServiceStatus }) {
  const label = status === 'checking' ? 'Checking' : status === 'available' ? 'Available' : 'Unavailable'

  return (
    <span className={`status-label status-${status}`}>
      <span className="status-dot" />
      {label}
    </span>
  )
}

function App() {
  const [apiStatus, setApiStatus] = useState<ServiceStatus>('checking')
  const [databaseStatus, setDatabaseStatus] = useState<ServiceStatus>('checking')
  const [redisStatus, setRedisStatus] = useState<ServiceStatus>('checking')
  const [lastChecked, setLastChecked] = useState<Date | null>(null)
  const [refreshCount, setRefreshCount] = useState(0)

  useEffect(() => {
    let active = true

    async function checkServices() {
      try {
        const response = await fetch(`${apiBaseUrl}/health`, { signal: AbortSignal.timeout(4000) })
        if (!response.ok) throw new Error('API health check failed')
        if (active) setApiStatus('available')
      } catch {
        if (active) setApiStatus('unavailable')
      }

      try {
        const response = await fetch(`${apiBaseUrl}/ready`, { signal: AbortSignal.timeout(4000) })
        const report = (await response.json()) as ReadinessReport
        if (active) {
          setDatabaseStatus(report.checks.database)
          setRedisStatus(report.checks.redis)
        }
      } catch {
        if (active) {
          setDatabaseStatus('unavailable')
          setRedisStatus('unavailable')
        }
      }

      if (active) setLastChecked(new Date())
    }

    void checkServices()
    const intervalId = window.setInterval(() => void checkServices(), 15000)

    return () => {
      active = false
      window.clearInterval(intervalId)
    }
  }, [refreshCount])

  const ready = apiStatus === 'available' && databaseStatus === 'available' && redisStatus === 'available'

  return (
    <div className="min-h-screen">
      <header className="topbar">
        <a className="brand" href="#overview" aria-label="ReplyAI home">
          <span className="brand-mark"><MessageCircle size={19} strokeWidth={2.4} /></span>
          <span>reply<span className="brand-accent">ai</span></span>
        </a>
        <div className="topbar-meta">
          <span className="environment-tag"><span />Local development</span>
          <span className="topbar-divider" />
          <span className="phase-label">Phase 01 <span>/</span> Foundation</span>
        </div>
      </header>

      <main id="overview" className="workspace">
        <section className="intro">
          <div className="intro-copy">
            <p className="eyebrow"><span className="eyebrow-line" />WORKSPACE STATUS</p>
            <h1>A clear foundation<br className="desktop-break" /> for thoughtful replies.</h1>
            <p className="intro-description">
              The local app and API are in place. Connectors and reply workflows are intentionally out of scope for now.
            </p>
          </div>
          <div className={`overall-status ${ready ? 'is-ready' : ''}`} aria-live="polite">
            <span className="overall-icon">{ready ? <Check size={20} /> : <Activity size={20} />}</span>
            <span>
              <strong>{ready ? 'All systems ready' : apiStatus === 'checking' ? 'Checking services' : 'Setup in progress'}</strong>
              <small>{ready ? 'Local dependencies are responding' : 'Some services are not connected'}</small>
            </span>
          </div>
        </section>

        <section className="system-panel" aria-labelledby="system-heading">
          <div className="panel-heading">
            <div className="panel-title">
              <span className="section-icon"><Radio size={17} /></span>
              <div>
                <h2 id="system-heading">Local services</h2>
                <p>Live checks from the ReplyAI API</p>
              </div>
            </div>
            <button className="icon-button" type="button" title="Refresh service checks" aria-label="Refresh service checks" onClick={() => setRefreshCount((count) => count + 1)}>
              <RefreshCw size={16} />
            </button>
          </div>

          <div className="service-list">
            <div className="service-row">
              <div className="service-name"><span className="service-icon api-icon"><Activity size={17} /></span><span><strong>Backend API</strong><small>Application liveness</small></span></div>
              <StatusLabel status={apiStatus} />
            </div>
            <div className="service-row">
              <div className="service-name"><span className="service-icon database-icon"><Database size={17} /></span><span><strong>PostgreSQL</strong><small>Readiness dependency</small></span></div>
              <StatusLabel status={databaseStatus} />
            </div>
            <div className="service-row">
              <div className="service-name"><span className="service-icon redis-icon"><Radio size={17} /></span><span><strong>Redis</strong><small>Readiness dependency</small></span></div>
              <StatusLabel status={redisStatus} />
            </div>
          </div>

          <div className="panel-footer">
            <span><Clock3 size={14} />{lastChecked ? `Last checked ${lastChecked.toLocaleTimeString()}` : 'Waiting for first check'}</span>
            <a href={`${apiBaseUrl}/docs`} target="_blank" rel="noreferrer">API docs <ArrowUpRight size={13} /></a>
          </div>
        </section>

        <section className="capabilities" aria-labelledby="capabilities-heading">
          <div className="capabilities-heading">
            <div>
              <p className="eyebrow"><span className="eyebrow-line" />PRODUCT SCOPE</p>
              <h2 id="capabilities-heading">Not available in this phase</h2>
            </div>
            <span className="scope-note"><CircleHelp size={15} /> No channel data is loaded</span>
          </div>
          <div className="capability-list">
            {capabilities.map(({ icon: Icon, name, detail, status }) => (
              <div className="capability-row" key={name}>
                <span className="capability-icon"><Icon size={17} /></span>
                <span className="capability-copy"><strong>{name}</strong><small>{detail}</small></span>
                <span className="unavailable-tag">{status}</span>
              </div>
            ))}
          </div>
        </section>

        <footer className="page-footer">
          <span>ReplyAI <span className="footer-separator">/</span> Local foundation</span>
          <span>Nothing is fetched, generated, or published in this build.</span>
        </footer>
      </main>
    </div>
  )
}

export default App
