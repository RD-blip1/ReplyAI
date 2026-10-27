import { useCallback, useEffect, useState } from 'react'
import { apiRequest, messageOf } from '../lib/api'

type SimComment = {
  id: string; video_id: string; scenario: string; text: string; author_name: string
  published_at: string; updated_at: string
  replies: { id: string; text: string; by?: string }[]
}
type SimState = {
  videos: { id: string; title: string }[]
  comments: Record<string, SimComment>
  inserted: { parent: string; reply_id: string; text: string; at: string }[]
}

const SCENARIOS = ['safe', 'disagree', 'spam', 'harassment', 'off_topic', 'generic', 'url', 'pii', 'promise',
  'sarcasm', 'creator_only', 'low_quality', 'rate_limit', 'auth_fail', 'hindi', 'hinglish', 'hidden_chars']

// Dev-only controls for the simulated YouTube channel (backend app/simulation.py).
export default function SimulationPanel({ csrf }: { csrf: string }) {
  const [state, setState] = useState<SimState | null>(null)
  const [video, setVideo] = useState('simQueue001')
  const [scenario, setScenario] = useState('safe')
  const [text, setText] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    try { setState(await apiRequest<SimState>('/api/simulation/state')) }
    catch (cause) { setNotice(messageOf(cause, 'Simulation state unavailable.')) }
  }, [])
  useEffect(() => { void load() }, [load])

  async function post(path: string, body?: unknown, done?: string) {
    setNotice(null)
    try {
      await apiRequest(path, { method: 'POST', headers: { 'X-CSRF-Token': csrf },
        body: body === undefined ? undefined : JSON.stringify(body) })
      if (done) setNotice(done)
      await load()
    } catch (cause) { setNotice(messageOf(cause, 'Simulation action failed.')) }
  }

  const comments = Object.values(state?.comments ?? {}).sort((a, b) => b.published_at.localeCompare(a.published_at))
  return <div className="mt-2 space-y-3 text-text-main" aria-label="Simulation controls">
    <div className="flex flex-wrap items-end gap-2">
      <label className="text-xs">Video
        <select aria-label="Simulated video" value={video} onChange={e => setVideo(e.target.value)}
          className="block rounded border border-dark-600 bg-dark-900 px-2 py-1">
          {(state?.videos ?? []).map(v => <option key={v.id} value={v.id}>{v.title}</option>)}
        </select></label>
      <label className="text-xs">Scenario
        <select aria-label="Scenario" value={scenario} onChange={e => setScenario(e.target.value)}
          className="block rounded border border-dark-600 bg-dark-900 px-2 py-1">
          {SCENARIOS.map(s => <option key={s} value={s}>{s}</option>)}
        </select></label>
      <label className="text-xs flex-1 min-w-[16rem]">Comment text
        <input aria-label="Simulated comment text" value={text} onChange={e => setText(e.target.value)}
          placeholder="Type a viewer comment" className="block w-full rounded border border-dark-600 bg-dark-900 px-2 py-1" /></label>
      <button type="button" disabled={!text.trim()} className="px-3 py-1 rounded bg-brand text-white text-xs disabled:opacity-50"
        onClick={() => void post('/api/simulation/comments', { video_id: video, text, scenario },
          'New simulated comment posted (discovery will find it on its next poll).').then(() => setText(''))}>
        Post as viewer</button>
      <button type="button" className="px-3 py-1 rounded border border-dark-600 text-xs"
        onClick={() => { if (window.confirm('Reset the simulated channel to its seed comments? Database records are kept.')) void post('/api/simulation/reset', undefined, 'Simulated channel reset.') }}>
        Reset channel</button>
      <button type="button" className="px-3 py-1 rounded border border-dark-600 text-xs" onClick={() => void load()}>Refresh</button>
    </div>
    {notice && <p role="status" className="text-xs">{notice}</p>}
    <details>
      <summary className="cursor-pointer text-xs">Simulated comments ({comments.length}) · simulated replies posted by ReplyAI: {state?.inserted.length ?? 0}</summary>
      <ul className="mt-2 max-h-72 overflow-auto divide-y divide-dark-600 text-xs">
        {comments.map(c => <li key={c.id} className="py-1.5 flex flex-wrap items-center gap-2">
          <code className="text-[10px]">{c.scenario}</code>
          <span className="flex-1 min-w-[12rem] truncate" title={c.text}>{c.text}</span>
          <span className="text-text-muted">{c.replies.length} repl.</span>
          <button type="button" className="underline" onClick={() => {
            const next = window.prompt('Edit comment text (simulates the viewer editing it):', c.text)
            if (next && next !== c.text) void post(`/api/simulation/comments/${c.id}/edit`, { text: next }, 'Comment edited.')
          }}>Edit</button>
          <button type="button" className="underline" onClick={() => void post(`/api/simulation/comments/${c.id}/creator-reply`,
            { text: 'Thanks! (typed by the creator in YouTube Studio)' }, 'Creator reply added outside ReplyAI.')}>Creator reply</button>
          <button type="button" className="underline" onClick={() => void post(`/api/simulation/comments/${c.id}/delete`, undefined, 'Comment deleted.')}>Delete</button>
        </li>)}
      </ul>
    </details>
  </div>
}
