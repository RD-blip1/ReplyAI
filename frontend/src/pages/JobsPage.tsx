import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, Send, XCircle, RotateCcw, ListChecks, Inbox } from 'lucide-react';
import { apiRequest, messageOf } from '../lib/api';
import type { EnqueueResult, JobList, JobStats, JobStatus, ReplyJob } from '../lib/types';
import { useAppData } from '../hooks/useAppData';
import { CLASS_LABELS, CLASS_STYLES } from '../lib/decisions';
import type { DecisionClass } from '../lib/types';

const PAGE_LIMIT = 200;
const POLL_MS = 4000;

const ACTIVE_STATUSES: JobStatus[] = ['pending', 'retry', 'processing', 'verified', 'publish_claimed'];
const CANCELLABLE_STATUSES: JobStatus[] = ['pending', 'retry'];
const REQUEUEABLE_STATUSES: JobStatus[] = ['failed', 'review', 'skipped', 'cancelled', 'needs_reauth', 'unknown'];

const STATUS_LABELS: Record<JobStatus, string> = {
  pending: 'Queued',
  retry: 'Waiting',
  processing: 'Running',
  verified: 'Ready to publish',
  publish_claimed: 'Publishing',
  published: 'Published',
  review: 'Needs review',
  skipped: 'Skipped',
  failed: 'Failed',
  unknown: 'Unconfirmed',
  needs_reauth: 'Reconnect needed',
  cancelled: 'Cancelled',
};

const STATUS_CLASSES: Record<JobStatus, string> = {
  pending: 'bg-dark-600 text-text-main border-dark-500',
  retry: 'bg-warning/20 text-warning border-warning/30',
  processing: 'bg-brand/20 text-brand-light border-brand/30',
  verified: 'bg-brand/10 text-brand-light border-brand/20',
  publish_claimed: 'bg-brand/20 text-brand-light border-brand/30',
  published: 'bg-success/20 text-success border-success/30',
  review: 'bg-brand/20 text-brand-light border-brand/30',
  skipped: 'bg-dark-600 text-text-muted border-dark-500',
  failed: 'bg-danger/20 text-danger border-danger/30',
  unknown: 'bg-warning/20 text-warning border-warning/30',
  needs_reauth: 'bg-danger/20 text-danger border-danger/30',
  cancelled: 'bg-dark-600 text-text-muted border-dark-500',
};

const SUMMARY_CARDS: { label: string; statuses: JobStatus[]; color: string; bg: string }[] = [
  { label: 'Queued', statuses: ['pending', 'retry'], color: 'text-warning', bg: 'bg-warning-bg' },
  { label: 'Working', statuses: ['processing', 'verified', 'publish_claimed'], color: 'text-brand-light', bg: 'bg-brand/10' },
  { label: 'Published', statuses: ['published'], color: 'text-success', bg: 'bg-success-bg' },
  { label: 'Needs review', statuses: ['review'], color: 'text-brand-light', bg: 'bg-brand/10' },
  { label: 'Failed / unknown', statuses: ['failed', 'unknown', 'needs_reauth'], color: 'text-danger', bg: 'bg-danger-bg' },
  { label: 'Skipped', statuses: ['skipped', 'cancelled'], color: 'text-text-muted', bg: 'bg-dark-700' },
];

function errorCode(job: ReplyJob): string | null {
  const code = job.last_error?.code;
  return typeof code === 'string' ? code : null;
}

function decision(job: ReplyJob): string | null {
  const value = job.outcome?.decision;
  if (typeof value === 'string') return value;
  const waiting = job.outcome?.waiting_for;
  return typeof waiting === 'string' ? `waiting: ${waiting}` : null;
}

export default function JobsPage({ appData }: { appData: ReturnType<typeof useAppData> }) {
  const { session, sessionLoading, sessionError } = appData;
  const [jobs, setJobs] = useState<ReplyJob[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<JobStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<JobStatus | ''>('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [enqueueOpen, setEnqueueOpen] = useState(false);
  const [enqueueVideo, setEnqueueVideo] = useState('');
  const [enqueueComments, setEnqueueComments] = useState('');
  const requestSequence = useRef(0);

  const csrf = session?.csrf_token ?? '';

  const load = useCallback(async () => {
    if (!session?.connected || !csrf) return;
    const sequence = ++requestSequence.current;
    setLoading(true);
    setError(null);
    try {
      const query = statusFilter ? `?limit=${PAGE_LIMIT}&status_filter=${statusFilter}` : `?limit=${PAGE_LIMIT}`;
      const [list, summary] = await Promise.all([
        apiRequest<JobList>(`/api/batch/jobs${query}`),
        apiRequest<JobStats>('/api/batch/jobs/stats/summary'),
      ]);
      if (sequence !== requestSequence.current) return;
      setJobs(list.items);
      setTotal(list.total);
      setStats(summary);
    } catch (cause) {
      if (sequence !== requestSequence.current) return;
      setError(messageOf(cause, 'The job queue could not be loaded.'));
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }, [session?.connected, csrf, statusFilter]);

  useEffect(() => {
    if (sessionLoading) return;
    if (!session?.connected) {
      requestSequence.current += 1;
      return;
    }
    void load();
  }, [load, session?.connected, sessionLoading]);

  const active = useMemo(() => (stats ? ACTIVE_STATUSES.some(s => (stats.counts[s] ?? 0) > 0) : false), [stats]);

  useEffect(() => {
    if (!session?.connected || !active) return;
    const id = window.setInterval(() => { void load(); }, POLL_MS);
    return () => window.clearInterval(id);
  }, [active, load, session?.connected]);

  const post = useCallback(async (path: string, body?: unknown, success?: string) => {
    if (!csrf) return;
    setBusy(true);
    setNotice(null);
    try {
      await apiRequest(path, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (success) setNotice(success);
      await load();
    } catch (cause) {
      setNotice(messageOf(cause, 'The action could not be completed.'));
    } finally {
      setBusy(false);
    }
  }, [csrf, load]);

  const cancelJob = (job: ReplyJob) => post(`/api/batch/jobs/${job.id}/cancel`, undefined, 'Job cancelled.');
  const requeueJob = (job: ReplyJob) => post(`/api/batch/jobs/${job.id}/requeue`, undefined, 'Job requeued.');
  const reconcile = () => post('/api/batch/jobs/reconcile', undefined, 'Interrupted jobs reconciled.');

  const submitEnqueue = async (event: React.FormEvent) => {
    event.preventDefault();
    // One id per line or comma; blank entries are dropped by the backend too, but we
    // trim here so the count in the notice matches what was actually sent.
    const comment_ids = enqueueComments.split(/[\s,]+/).map(v => v.trim()).filter(Boolean);
    if (!enqueueVideo.trim() || comment_ids.length === 0) {
      setNotice('Enter a video id and at least one comment id.');
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const result = await apiRequest<EnqueueResult>('/api/batch/jobs/enqueue', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf },
        body: JSON.stringify({ video_id: enqueueVideo.trim(), comment_ids }),
      });
      setNotice(`Enqueued ${result.created.length} job(s); ${result.duplicates} already queued.`);
      setEnqueueComments('');
      await load();
    } catch (cause) {
      setNotice(messageOf(cause, 'The jobs could not be enqueued.'));
    } finally {
      setBusy(false);
    }
  };

  const summary = useMemo(() => SUMMARY_CARDS.map(card => ({
    ...card,
    count: card.statuses.reduce((sum, s) => sum + (stats?.counts[s] ?? 0), 0),
  })), [stats]);

  const visible = statusFilter ? jobs.filter(j => j.status === statusFilter) : jobs;

  return (
    <div className="flex flex-col h-full gap-6 animate-in fade-in duration-300">
      <header className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight mb-1">Reply jobs</h1>
          <p className="text-text-muted text-sm">Background generation and publishing, with retries and recovery.</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className="flex gap-2">
            <button
              type="button"
              disabled={loading || !session?.connected}
              onClick={reconcile}
              className="flex items-center gap-2 px-4 py-2 rounded-full border border-dark-600 bg-dark-800/80 hover:bg-dark-700 text-sm font-medium transition-colors disabled:opacity-50"
              title="Recover jobs whose worker stopped mid-run"
            >
              <ListChecks className="w-4 h-4" />
              Reconcile
            </button>
            <button
              type="button"
              disabled={loading || !session?.connected}
              onClick={load}
              className="flex items-center gap-2 px-4 py-2 rounded-full border border-dark-600 bg-dark-800/80 hover:bg-dark-700 text-sm font-medium transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
          </div>
          {stats && (
            <span className="text-xs text-text-muted">
              {stats.total} total · {stats.due_now} due now{active ? ' · auto-refreshing' : ''}
            </span>
          )}
        </div>
      </header>

      {(error || sessionError) && (
        <div className="bg-danger-bg border border-danger/30 rounded-xl p-4 text-danger flex gap-3 items-start">
          <XCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
          <div>
            <h3 className="font-semibold mb-1">Error loading jobs</h3>
            <p className="text-sm opacity-90">{error || sessionError}</p>
          </div>
        </div>
      )}

      {notice && (
        <div className="rounded-xl border border-dark-600 bg-dark-800/60 px-4 py-3 text-sm text-text-main" role="status">
          {notice}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
        {summary.map(card => (
          <div
            key={card.label}
            className="flex items-center gap-3 p-4 rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm"
          >
            <div className={`w-10 h-10 rounded-full flex items-center justify-center ${card.bg} ${card.color}`}>
              <Send className="w-5 h-5" />
            </div>
            <div>
              <div className="text-xl font-bold">{card.count}</div>
              <div className="text-xs text-text-muted">{card.label}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex flex-col sm:flex-row gap-4 justify-between items-center">
        <button
          type="button"
          onClick={() => setEnqueueOpen(open => !open)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg border border-dark-600 bg-dark-900 hover:bg-dark-800 text-sm font-medium transition-colors"
        >
          <Inbox className="w-4 h-4" />
          {enqueueOpen ? 'Hide enqueue form' : 'Enqueue jobs'}
        </button>
        <select
          value={statusFilter}
          onChange={e => setStatusFilter(e.target.value as JobStatus | '')}
          aria-label="Filter by status"
          className="px-4 py-2 bg-dark-900 border border-dark-600 rounded-lg text-sm focus:outline-none focus:border-brand"
        >
          <option value="">All statuses</option>
          {Object.entries(STATUS_LABELS).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </div>

      {enqueueOpen && (
        <form onSubmit={submitEnqueue} className="rounded-2xl border border-dark-600 bg-dark-800/50 p-4 grid gap-3 md:grid-cols-[1fr_2fr_auto] md:items-end">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-text-muted">Video id</span>
            <input
              value={enqueueVideo}
              onChange={e => setEnqueueVideo(e.target.value)}
              placeholder="abcdefghijk"
              aria-label="Video id"
              className="px-3 py-2 bg-dark-900 border border-dark-600 rounded-lg text-sm focus:outline-none focus:border-brand"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-text-muted">Comment ids (comma or newline separated, max 100)</span>
            <textarea
              value={enqueueComments}
              onChange={e => setEnqueueComments(e.target.value)}
              rows={2}
              aria-label="Comment ids"
              className="px-3 py-2 bg-dark-900 border border-dark-600 rounded-lg text-sm focus:outline-none focus:border-brand"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !session?.connected}
            className="h-10 px-5 rounded-lg bg-brand text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            Enqueue
          </button>
        </form>
      )}

      <div className="flex-1 rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm overflow-hidden flex flex-col min-h-[300px]">
        <div className="grid grid-cols-[2fr_1.2fr_1fr_1.5fr_auto] gap-4 p-4 border-b border-dark-600 bg-dark-900/50 text-sm font-medium text-text-muted">
          <div>Comment</div>
          <div>Status</div>
          <div>Attempts</div>
          <div>Latest note</div>
          <div className="text-right">Actions</div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {loading && visible.length === 0 ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center">Loading jobs…</div>
          ) : !session?.connected ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center">
              {sessionError || 'Connect a YouTube channel to view reply jobs.'}
            </div>
          ) : visible.length === 0 ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center flex-col gap-2">
              <ListChecks className="w-10 h-10 text-dark-500" />
              <p>No jobs {statusFilter ? 'with this status' : 'queued yet'}.</p>
            </div>
          ) : (
            visible.map(job => (
              <div key={job.id} className="grid grid-cols-[2fr_1.2fr_1fr_1.5fr_auto] gap-4 p-4 border-b border-dark-600/50 hover:bg-dark-700/30 transition-colors text-sm items-center">
                <div className="min-w-0">
                  <div className="font-mono text-xs text-text-muted truncate">video {job.video_id}</div>
                  <div className="font-mono text-xs text-text-dim truncate" title={job.comment_id}>comment {job.comment_id}</div>
                  {job.batch_id && <div className="text-[11px] text-text-muted/70 truncate">batch {job.batch_id}</div>}
                </div>
                <div>
                  <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border ${STATUS_CLASSES[job.status]}`}>
                    {STATUS_LABELS[job.status] ?? job.status}
                  </span>
                </div>
                <div className="text-text-muted">
                  {job.attempts}/{job.max_attempts}
                  {job.recoveries > 0 && <span className="text-xs ml-1">(+{job.recoveries} recovered)</span>}
                </div>
                <div className="text-text-dim min-w-0">
                  <div className="truncate" title={errorCode(job) ?? undefined}>{errorCode(job) ?? '—'}</div>
                  {decision(job) && <div className="text-[11px] text-text-muted truncate">{decision(job)}</div>}
                  {typeof job.outcome?.decision_class === 'string' && job.outcome.decision_class in CLASS_LABELS && (
                    <span data-testid="decision-class" className={`mt-1 inline-block rounded-full border px-2 py-0.5 text-[10px] font-semibold ${CLASS_STYLES[job.outcome.decision_class as DecisionClass]}`}>
                      {CLASS_LABELS[job.outcome.decision_class as DecisionClass]}</span>)}
                </div>
                <div className="flex justify-end gap-2">
                  {CANCELLABLE_STATUSES.includes(job.status) && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => cancelJob(job)}
                      className="px-3 py-1.5 rounded-lg border border-dark-600 hover:bg-dark-700 text-xs disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  )}
                  {REQUEUEABLE_STATUSES.includes(job.status) && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => requeueJob(job)}
                      aria-label={`Requeue job for ${job.comment_id}`}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-dark-600 hover:bg-dark-700 text-xs disabled:opacity-50"
                    >
                      <RotateCcw className="w-3 h-3" />
                      Requeue
                    </button>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
        {statusFilter && jobs.length !== total && (
          <div className="p-3 text-xs text-text-muted border-t border-dark-600">
            Showing {jobs.length} of {total} jobs (most recent first).
          </div>
        )}
      </div>
    </div>
  );
}
