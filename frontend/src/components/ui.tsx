import type { ComponentType, ReactNode } from 'react'

// Small shared building blocks for the page layouts. Styling lives in index.css
// (.page-header, .kpi, .avatar, ...) so every page draws from the same tokens.

export function PageHeader({ eyebrow, title, subtitle, actions, meta, titleIcon }: {
  eyebrow?: string
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  meta?: ReactNode
  titleIcon?: ReactNode
}) {
  return <header className="page-header">
    <div className="min-w-0">
      {eyebrow ? <p className="eyebrow mb-2">{eyebrow}</p> : null}
      <h1 className="page-title flex items-center gap-3">{titleIcon}{title}</h1>
      {subtitle ? <p className="page-subtitle">{subtitle}</p> : null}
    </div>
    {actions || meta ? <div className="page-actions">
      {actions ? <div className="flex flex-wrap justify-end gap-2">{actions}</div> : null}
      {meta ? <span className="page-actions-meta">{meta}</span> : null}
    </div> : null}
  </header>
}

const AVATAR_COLORS = ['#2ee6a6', '#86cff5', '#c8a8f7', '#f4c56f', '#f59b8f', '#7fe0d4']

// Deterministic colour per name so the same commenter keeps the same circle.
function colorFor(name: string) {
  let hash = 0
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) | 0
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length]
}

function initialOf(name: string) {
  const match = name.match(/[\p{L}\p{N}]/u)
  return (match ? match[0] : '?').toUpperCase()
}

export function Avatar({ name, src, size = 40, colorKey, color }: {
  name: string; src?: string | null; size?: number; colorKey?: string; color?: string
}) {
  const style = { width: size, height: size, fontSize: Math.round(size * 0.4) }
  if (src) {
    return <span className="avatar" style={style}><img src={src} alt="" /></span>
  }
  return <span className="avatar" aria-hidden="true"
    style={{ ...style, background: color ?? colorFor(colorKey ?? name) }}>{initialOf(name)}</span>
}

export type Tone = 'green' | 'amber' | 'red' | 'neutral'

export function KpiBody({ icon: Icon, tone = 'neutral', value, label, testId }: {
  icon: ComponentType<{ className?: string }>
  tone?: Tone
  value: ReactNode
  label: ReactNode
  testId?: string
}) {
  return <>
    <span className={`icon-tile ${tone === 'neutral' ? '' : `icon-tile-${tone}`}`} aria-hidden="true">
      <Icon />
    </span>
    <span className="min-w-0">
      <span className="kpi-value block" data-testid={testId}>{value}</span>
      <span className="kpi-label block">{label}</span>
    </span>
  </>
}

export function Kpi(props: Parameters<typeof KpiBody>[0]) {
  return <div className="kpi"><KpiBody {...props} /></div>
}
