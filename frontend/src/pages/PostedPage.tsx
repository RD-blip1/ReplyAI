import { useEffect, useState, useMemo, useCallback, useRef } from 'react';
import { formatDateTime, formatTime } from '../lib/format';
import { RefreshCw, Search, CheckCircle2, XCircle, Clock, History, ExternalLink } from 'lucide-react';
import { Kpi, PageHeader } from '../components/ui';
import { apiRequest } from '../lib/api';
import type { PublicationPage } from '../lib/types';
import { useAppData } from '../hooks/useAppData';

const PAGE_SIZE = 100;

export default function PostedPage({ appData }: { appData: ReturnType<typeof useAppData> }) {
  const { session, sessionLoading, sessionError, videos, comments } = appData;
  const [result, setResult] = useState<{ channelId: string; data: PublicationPage } | null>(null);
  const [requestError, setRequestError] = useState<{ channelId: string; message: string } | null>(null);
  const [requestLoading, setRequestLoading] = useState(false);
  const [lastSync, setLastSync] = useState<Date | null>(null);
  const [activeTab, setActiveTab] = useState<'All' | 'Posted' | 'Failed' | 'Unconfirmed'>('All');
  const [search, setSearch] = useState('');
  const requestSequence = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const channelId = session?.channel?.channel_id ?? null;
  const data = channelId && result?.channelId === channelId ? result.data : null;
  const error = channelId
    ? requestError?.channelId === channelId ? requestError.message : null
    : sessionError;
  const loading = sessionLoading || Boolean(session?.connected && (requestLoading || (!data && !error)));

  // Video titles and comment author/text come from data the app already holds for this
  // channel, so history stays readable without another YouTube request.
  const videoTitles = useMemo(
    () => new Map((videos ?? []).map(video => [video.video_id, video])),
    [videos],
  );
  const commentByText = useMemo(() => {
    const map = new Map<string, (typeof comments)[number]>();
    for (const comment of comments ?? []) {
      if (comment.comment_id) map.set(comment.comment_id, comment);
    }
    return map;
  }, [comments]);

  const fetchPublications = useCallback(async () => {
    if (!session?.connected || !session.channel?.channel_id) return;
    const requestedChannelId = session.channel.channel_id;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    const sequence = ++requestSequence.current;
    setRequestLoading(true);
    setRequestError(null);
    try {
      const items: PublicationPage['items'] = [];
      let total = 0;
      let offset = 0;

      do {
        const page = await apiRequest<PublicationPage>(
          `/api/youtube/publications?limit=${PAGE_SIZE}&offset=${offset}`,
          { signal: controller.signal },
        );
        if (sequence !== requestSequence.current) return;
        items.push(...page.items);
        total = page.total;
        offset += page.items.length;
        if (offset < total && page.items.length === 0) {
          throw new Error('Publication history returned an incomplete page. Retry to load it again.');
        }
      } while (offset < total);

      if (sequence !== requestSequence.current) return;
      const uniqueItems = [...new Map(items.map(item => [item.id, item])).values()];
      setResult({ channelId: requestedChannelId, data: { items: uniqueItems, total } });
      setLastSync(new Date());
    } catch (cause: unknown) {
      if (controller.signal.aborted || sequence !== requestSequence.current) return;
      setRequestError({
        channelId: requestedChannelId,
        message: cause instanceof Error ? cause.message : 'Failed to load publication history.',
      });
    } finally {
      if (sequence === requestSequence.current) setRequestLoading(false);
    }
  }, [session]);

  useEffect(() => {
    if (sessionLoading || !session?.connected) {
      requestController.current?.abort();
      requestSequence.current += 1;
      return;
    }

    void fetchPublications();
    return () => {
      requestSequence.current += 1;
      requestController.current?.abort();
    };
  }, [fetchPublications, session?.channel?.channel_id, session?.connected, sessionLoading]);

  const filteredItems = useMemo(() => {
    if (!data) return [];
    return data.items.filter(item => {
      if (activeTab === 'Posted' && item.status !== 'posted') return false;
      if (activeTab === 'Failed' && item.status !== 'failed') return false;
      if (activeTab === 'Unconfirmed' && (item.status !== 'unknown' && item.status !== 'sending')) return false;
      if (search && !item.text.toLowerCase().includes(search.trim().toLowerCase())) return false;
      return true;
    });
  }, [data, activeTab, search]);

  const stats = useMemo(() => {
    if (!data) return { posted: '--', failed: '--', unconfirmed: '--' };
    let p = 0, f = 0, u = 0;
    data.items.forEach(i => {
      if (i.status === 'posted') p++;
      else if (i.status === 'failed') f++;
      else if (i.status === 'unknown' || i.status === 'sending') u++;
    });
    return { posted: p.toString(), failed: f.toString(), unconfirmed: u.toString() };
  }, [data]);

  const simulated = Boolean(session?.simulation);
  const cols = 'grid grid-cols-[minmax(0,1.35fr)_minmax(0,1.25fr)_10rem_11rem] gap-6';

  return (
    <div className="page">
      <PageHeader
        title="Posted replies"
        subtitle="Track your publishing history."
        actions={
          <button
            disabled={loading || !session?.connected}
            onClick={fetchPublications}
            className="btn btn-primary btn-lg"
          >
            <RefreshCw className={loading ? 'animate-spin' : ''} aria-hidden="true" />
            Refresh status
          </button>
        }
        meta={lastSync ? `Loaded ${formatTime(lastSync)}` : null}
      />

      {error && (
        <div className="alert alert-error">
          <XCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
          <div>
            <h3 className="mb-1 font-semibold">Publication history could not be loaded</h3>
            <p className="text-sm opacity-90">{error}</p>
            {session?.connected && (
              <button type="button" className="mt-3 underline" disabled={loading} onClick={() => void fetchPublications()}>
                Retry loading history
              </button>
            )}
          </div>
        </div>
      )}

      {/* Summary cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Kpi icon={CheckCircle2} tone="green" value={stats.posted} label="Posted" />
        <Kpi icon={XCircle} tone="red" value={stats.failed} label="Failed" />
        <Kpi icon={Clock} tone="amber" value={stats.unconfirmed} label="Unconfirmed" />
      </div>

      {simulated ? (
        <p className="alert alert-warning" role="note">
          Simulation mode: these replies were posted to the simulated channel only. None of them is on YouTube.
        </p>
      ) : null}

      {/* Filters */}
      <div className="flex flex-col items-stretch justify-between gap-3 sm:flex-row sm:items-center">
        <div className="relative w-full sm:max-w-sm">
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" aria-hidden="true" />
          <input
            type="text"
            placeholder="Search replies..."
            aria-label="Search replies"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="input pl-10"
          />
        </div>

        <div className="seg self-start sm:self-auto" role="group" aria-label="Filter by status">
          {(['All', 'Posted', 'Failed', 'Unconfirmed'] as const).map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              aria-pressed={activeTab === tab}
              className={`seg-item ${activeTab === tab ? 'is-active' : ''}`}
            >
              {tab}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        <div className="table-scroll"><div className="min-w-[56rem]">
        <div className={`data-head ${cols} px-5 py-4`}>
          <div>Comment &amp; context</div>
          <div>Your reply</div>
          <div>Status</div>
          <div>Last updated</div>
        </div>

        <div>
          {loading ? (
            <div className="empty-state" role="status">Loading publication history…</div>
          ) : !session?.connected ? (
            <div className="empty-state">
              {sessionError || 'Connect a YouTube channel to view publication history.'}
            </div>
          ) : error && !data ? (
            <div className="empty-state">Publication history could not be loaded. Use Retry loading history above.</div>
          ) : data?.items.length === 0 ? (
            <div className="empty-state">
              <History aria-hidden="true" />
              <p>No publication history yet.</p>
            </div>
          ) : filteredItems.length === 0 ? (
            <div className="empty-state">
              <History aria-hidden="true" />
              <p>No publication history matches your filters.</p>
            </div>
          ) : (
            filteredItems.map(item => {
              const video = videoTitles.get(item.video_id);
              const comment = commentByText.get(item.parent_comment_id);
              const commentText = item.comment_text ?? comment?.text ?? null;
              const videoTitle = comment?.video_title ?? video?.title ?? null;
              const author = comment?.author_name ?? null;
              return (
              <div key={item.id} className={`data-row ${cols} px-5 py-5 text-[0.9375rem]`}>
                <div className="min-w-0">
                  {videoTitle ? (
                    <div className="mb-1 truncate text-base font-bold text-text-main" title={videoTitle}>{videoTitle}</div>
                  ) : (
                    <div className="mono-meta mb-1">Video: {item.video_id}</div>
                  )}
                  {commentText ? (
                    <div className="line-clamp-2 text-text-dim [overflow-wrap:anywhere]" title={commentText}>{commentText}</div>
                  ) : (
                    <div className="text-text-muted">Comment text unavailable</div>
                  )}
                  {author && <div className="mt-1 text-sm text-text-muted">Comment by {author}</div>}
                  <div className="mono-meta mt-1 truncate" title={`video ${item.video_id} · comment ${item.parent_comment_id}`}>video {item.video_id} · comment {item.parent_comment_id}</div>
                </div>
                <div className="min-w-0">
                  <div className="line-clamp-4 text-text-main [overflow-wrap:anywhere]">{item.text}</div>
                  {item.draft_revision && <div className="mt-1 text-sm text-text-muted">Rev {item.draft_revision}</div>}
                </div>
                <div className="min-w-0">
                  {item.status === 'posted' && (
                    <div className="flex flex-col items-start gap-2">
                      <span className="badge badge-green">{simulated ? 'Posted (simulated)' : 'Posted'}</span>
                      {item.reply_comment_id && !simulated && (
                        <a href={`https://youtube.com/comments?lc=${item.reply_comment_id}`} target="_blank" rel="noreferrer" className="link inline-flex items-center gap-2 text-sm">View on YouTube<ExternalLink className="h-3.5 w-3.5" aria-hidden="true" /></a>
                      )}
                      {simulated && <span className="text-sm text-text-muted">Not on YouTube</span>}
                    </div>
                  )}
                  {item.status === 'failed' && (
                    <div className="flex flex-col items-start gap-2">
                      <span className="badge badge-red">Failed</span>
                      {item.failure?.message && <span className="text-sm text-danger [overflow-wrap:anywhere]" title={item.failure.message}>{item.failure.message}</span>}
                    </div>
                  )}
                  {(item.status === 'unknown' || item.status === 'sending') && (
                    <div className="flex flex-col items-start gap-2">
                      <span className="badge badge-amber">Unconfirmed</span>
                      <span className="text-sm text-text-muted">{simulated ? 'Check the simulated channel' : 'Check YouTube'}</span>
                    </div>
                  )}
                </div>
                <div className="text-text-dim tabular-nums">
                  {formatDateTime(item.resolved_at) ?? formatDateTime(item.created_at) ?? 'Unknown'}
                </div>
              </div>
              );
            })
          )}
        </div>
        </div></div>
      </div>
    </div>
  );
}
