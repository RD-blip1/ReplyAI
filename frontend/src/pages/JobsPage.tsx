import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, Send, XCircle, RotateCcw, ListChecks, Inbox, ChevronDown } from 'lucide-react';
import { Kpi, PageHeader, type Tone } from '../components/ui';
import { apiRequest, messageOf } from '../lib/api';
import type { EnqueueResult, JobList, JobStats, JobStatus, ReplyJob } from '../lib/types';
import { useAppData } from '../hooks/useAppData';
import { CLASS_LABELS, decisionBadge } from '../lib/decisions';
import type { DecisionClass } from '../lib/types';
import { jobNote } from '../lib/jobText';

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
  pending: 'badge',
  retry: 'badge badge-amber',
  processing: 'badge badge-green',
  verified: 'badge badge-green',
  publish_claimed: 'badge badge-green',
  published: 'badge badge-green',
  review: 'badge badge-amber',
  skipped: 'badge',
  failed: 'badge badge-red',
  unknown: 'badge badge-amber',
  needs_reauth: 'badge badge-red',
  cancelled: 'badge',
};

const SUMMARY_CARDS: { label: string; statuses: JobStatus[]; tone: Tone }[] = [
  { label: 'Queued', statuses: ['pending', 'retry'], tone: 'amber' },
  { label: 'Working', statuses: ['processing', 'verified', 'publish_claimed'], tone: 'neutral' },
  { label: 'Published', statuses: ['published'], tone: 'green' },
  { label: 'Needs review', statuses: ['review'], tone: 'amber' },
  { label: 'Failed / unknown', statuses: ['failed', 'unknown', 'needs_reauth'], tone: 'red' },
  { label: 'Skipped', statuses: ['skipped', 'cancelled'], tone: 'neutral' },
];

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
    <div className="page">
      <PageHeader
        title="Reply jobs"
        subtitle="Background generation and publishing, with retries and recovery."
        actions={<>
          <button
            type="button"
            disabled={loading || !session?.connected}
            onClick={reconcile}
            className="btn btn-lg"
            title="Recover jobs whose worker stopped mid-run"
          >
            <ListChecks aria-hidden="true" />
            Reconcile
          </button>
          <button
            type="button"
            disabled={loading || !session?.connected}
            onClick={load}
            className="btn btn-primary btn-lg"
          >
            <RefreshCw className={loading ? 'animate-spin' : ''} aria-hidden="true" />
            Refresh
          </button>
        </>}
        meta={stats ? `${stats.total} total · ${stats.due_now} due now${active ? ' · auto-refreshing' : ''}` : null}
      />

      {(error || sessionError) && (
        <div className="alert alert-error">
          <XCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
          <div>
            <h3 className="mb-1 font-semibold">Error loading jobs</h3>
            <p className="text-sm opacity-90">{error || sessionError}</p>
          </div>
        </div>
      )}

      {notice && (
        <div className="alert" role="status">
          {notice}
        </div>
      )}

      <div className="kpi-grid jobs-kpis">
        {summary.map(card => (
          <Kpi key={card.label} icon={Send} tone={card.tone} value={card.count} label={card.label} />
        ))}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <button
          type="button"
          onClick={() => setEnqueueOpen(open => !open)}
          aria-expanded={enqueueOpen}
          className="btn btn-lg"
        >
          <Inbox aria-hidden="true" />
          {enqueueOpen ? 'Hide enqueue form' : 'Enqueue jobs'}
        </button>
        <div className="relative w-full sm:w-56">
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value as JobStatus | '')}
            aria-label="Filter by status"
            className="select"
          >
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" aria-hidden="true" />
        </div>
      </div>

      {enqueueOpen && (
        <form onSubmit={submitEnqueue} className="card card-pad grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto] md:items-end">
          <label className="flex flex-col gap-2 text-sm">
            <span className="text-text-dim">Video id</span>
            <input
              value={enqueueVideo}
              onChange={e => setEnqueueVideo(e.target.value)}
              placeholder="abcdefghijk"
              aria-label="Video id"
              className="input font-mono"
            />
          </label>
          <label className="flex flex-col gap-2 text-sm">
            <span className="text-text-dim">Comment ids (comma or newline separated, max 100)</span>
            <textarea
              value={enqueueComments}
              onChange={e => setEnqueueComments(e.target.value)}
              rows={2}
              aria-label="Comment ids"
              className="textarea font-mono"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !session?.connected}
            className="btn btn-primary btn-lg"
          >
            Enqueue
          </button>
        </form>
      )}

      <div className="card flex min-h-[18rem] flex-col overflow-hidden">
        <div className="table-scroll flex flex-1 flex-col"><div className="flex min-w-[52rem] flex-1 flex-col">
        <div className="data-head grid grid-cols-[minmax(0,1.4fr)_9rem_5.5rem_minmax(0,2fr)_9rem] gap-4 px-5 py-4">
          <div>Comment</div>
          <div>Status</div>
          <div>Attempts</div>
          <div>Latest note</div>
          <div className="text-right">Actions</div>
        </div>
        <div className="flex-1">
          {loading && visible.length === 0 ? (
            <div className="empty-state" role="status">Loading jobs…</div>
          ) : !session?.connected ? (
            <div className="empty-state">
              {sessionError || 'Connect a YouTube channel to view reply jobs.'}
            </div>
          ) : visible.length === 0 ? (
            <div className="empty-state">
              <ListChecks aria-hidden="true" />
              <p>No jobs {statusFilter ? 'with this status' : 'queued yet'}.</p>
            </div>
          ) : (
            visible.map(job => (
              <div key={job.id} className="data-row grid grid-cols-[minmax(0,1.4fr)_9rem_5.5rem_minmax(0,2fr)_9rem] items-start gap-4 px-5 py-4 text-[0.9375rem]">
                <div className="mono-meta min-w-0">
                  <div className="truncate text-text-main" title={job.video_id}>video {job.video_id}</div>
                  <div className="truncate text-text-main" title={job.comment_id}>comment {job.comment_id}</div>
                  {job.batch_id && <div className="truncate" title={job.batch_id}>batch {job.batch_id}</div>}
                </div>
                <div>
                  <span className={STATUS_CLASSES[job.status]}>
                    {STATUS_LABELS[job.status] ?? job.status}
                  </span>
                </div>
                <div className="pt-0.5 font-mono text-sm text-text-dim tabular-nums">
                  {job.attempts}/{job.max_attempts}
                  {job.recoveries > 0 && <span className="block font-sans text-xs text-text-muted">+{job.recoveries} recovered</span>}
                </div>
                <div className="min-w-0 space-y-2 text-text-main">
                  {(() => {
                    const note = jobNote(job)
                    return note ? <>
                      <div className="line-clamp-2 break-words" title={note.text}>{note.text}</div>
                      {note.code ? <div className="mono-meta truncate" title={note.code}>{note.code}</div> : null}
                    </> : <div className="text-text-muted">—</div>
                  })()}
                  {typeof job.outcome?.decision_class === 'string' && job.outcome.decision_class in CLASS_LABELS && (() => {
                    const badge = decisionBadge(job.outcome.decision_class as DecisionClass, job.outcome.execution_result, job.outcome.decision)
                    return <span data-testid="decision-class" className={badge.style}>
                      {badge.label}</span>
                  })()}
                </div>
                <div className="flex flex-wrap justify-end gap-2">
                  {CANCELLABLE_STATUSES.includes(job.status) && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => cancelJob(job)}
                      className="btn btn-sm"
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
                      className="btn btn-sm"
                    >
                      <RotateCcw aria-hidden="true" />
                      Requeue
                    </button>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
        {statusFilter && jobs.length !== total && (
          <div className="border-t border-[var(--line)] px-5 py-3 text-sm text-text-muted">
            Showing {jobs.length} of {total} jobs (most recent first).
          </div>
        )}
        </div></div>
      </div>
    </div>
  );
}
