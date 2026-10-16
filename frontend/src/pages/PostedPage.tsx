import { useEffect, useState, useMemo, useCallback, useRef } from 'react';
import { RefreshCw, Search, CheckCircle2, XCircle, Clock, History } from 'lucide-react';
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

  return (
    <div className="flex flex-col h-full gap-6 animate-in fade-in duration-300">
      <header className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight mb-1">Posted replies</h1>
          <p className="text-text-muted text-sm">Track your publishing history.</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <button 
            disabled={loading || !session?.connected}
            onClick={fetchPublications}
            className="flex items-center gap-2 px-4 py-2 rounded-full border border-dark-600 bg-dark-800/80 hover:bg-dark-700 text-sm font-medium transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            Refresh status
          </button>
          {lastSync && <span className="text-xs text-text-muted">Loaded just now</span>}
        </div>
      </header>

      {error && (
        <div className="bg-danger-bg border border-danger/30 rounded-xl p-4 text-danger flex gap-3 items-start">
          <XCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
          <div>
            <h3 className="font-semibold mb-1">Error Loading Data</h3>
            <p className="text-sm opacity-90">{error}</p>
            {session?.connected && (
              <button type="button" className="mt-3 underline" disabled={loading} onClick={() => void fetchPublications()}>
                Retry loading history
              </button>
            )}
          </div>
        </div>
      )}

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {[
          { label: 'Posted', count: stats.posted, icon: CheckCircle2, color: 'text-success', bg: 'bg-success-bg' },
          { label: 'Failed', count: stats.failed, icon: XCircle, color: 'text-danger', bg: 'bg-danger-bg' },
          { label: 'Unconfirmed', count: stats.unconfirmed, icon: Clock, color: 'text-warning', bg: 'bg-warning-bg' },
        ].map(card => (
          <div key={card.label} className="flex items-center gap-4 p-5 rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm">
            <div className={`w-12 h-12 rounded-full flex items-center justify-center ${card.bg} ${card.color}`}>
              <card.icon className="w-6 h-6" />
            </div>
            <div>
              <div className="text-2xl font-bold">{card.count}</div>
              <div className="text-sm text-text-muted">{card.label}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Filter Area */}
      <div className="flex flex-col sm:flex-row gap-4 justify-between items-center mt-2">
        <div className="flex gap-4 w-full sm:w-auto">
          <div className="relative flex-1 sm:w-64">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
            <input 
              type="text" 
              placeholder="Search replies..." 
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="w-full pl-9 pr-4 py-2 bg-dark-900 border border-dark-600 rounded-lg text-sm focus:outline-none focus:border-brand"
            />
          </div>
        </div>
        
        <div className="flex bg-dark-900 rounded-lg p-1 border border-dark-600 w-full sm:w-auto overflow-x-auto">
          {(['All', 'Posted', 'Failed', 'Unconfirmed'] as const).map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-1.5 rounded-md text-sm font-medium whitespace-nowrap ${
                activeTab === tab ? 'bg-brand/20 text-brand-light' : 'text-text-muted hover:text-text-main'
              }`}
            >
              {tab}
            </button>
          ))}
        </div>
      </div>

      {/* Table Area */}
      <div className="flex-1 rounded-2xl border border-dark-600 bg-dark-800/50 backdrop-blur-sm overflow-hidden flex flex-col min-h-[300px]">
        <div className="grid grid-cols-[2fr_2fr_1fr_1.5fr] gap-4 p-4 border-b border-dark-600 bg-dark-900/50 text-sm font-medium text-text-muted">
          <div>Comment & context</div>
          <div>Your reply</div>
          <div>Status</div>
          <div>Last updated</div>
        </div>
        
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center">Loading publication history…</div>
          ) : !session?.connected ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center">
              {sessionError || 'Connect a YouTube channel to view publication history.'}
            </div>
          ) : error && !data ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center">Publication history could not be loaded. Use Retry loading history above.</div>
          ) : data?.items.length === 0 ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center flex-col gap-2">
              <History className="w-10 h-10 text-dark-500" />
              <p>No publication history yet.</p>
            </div>
          ) : filteredItems.length === 0 ? (
            <div className="flex h-full items-center justify-center text-text-muted p-8 text-center flex-col gap-2">
              <History className="w-10 h-10 text-dark-500" />
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
              <div key={item.id} className="grid grid-cols-[2fr_2fr_1fr_1.5fr] gap-4 p-4 border-b border-dark-600/50 hover:bg-dark-700/30 transition-colors text-sm">
                <div>
                  {videoTitle ? (
                    <div className="font-medium text-text-main mb-1 truncate" title={videoTitle}>{videoTitle}</div>
                  ) : (
                    <div className="font-mono text-xs text-text-muted mb-1">Video: {item.video_id}</div>
                  )}
                  {commentText ? (
                    <div className="text-text-dim line-clamp-2" title={commentText}>{commentText}</div>
                  ) : (
                    <div className="text-text-dim italic">Comment text unavailable</div>
                  )}
                  {author && <div className="text-xs text-text-muted mt-1">Comment by {author}</div>}
                  <div className="text-[11px] text-text-muted/70 mt-1 font-mono">video {item.video_id} · comment {item.parent_comment_id}</div>
                </div>
                <div>
                  <div className="text-text-main line-clamp-3">{item.text}</div>
                  {item.draft_revision && <div className="text-xs text-text-muted mt-1">Rev {item.draft_revision}</div>}
                </div>
                <div>
                  {item.status === 'posted' && (
                    <div className="flex flex-col gap-1 items-start">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-success/20 text-success border border-success/30">Posted</span>
                      {item.reply_comment_id && (
                        <a href={`https://youtube.com/comments?lc=${item.reply_comment_id}`} target="_blank" rel="noreferrer" className="text-xs text-brand hover:underline">View on YouTube</a>
                      )}
                    </div>
                  )}
                  {item.status === 'failed' && (
                    <div className="flex flex-col gap-1 items-start">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-danger/20 text-danger border border-danger/30">Failed</span>
                      {item.failure?.message && <span className="text-xs text-danger/80" title={item.failure.message}>Hover for reason</span>}
                    </div>
                  )}
                  {(item.status === 'unknown' || item.status === 'sending') && (
                    <div className="flex flex-col gap-1 items-start">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-warning/20 text-warning border border-warning/30">Unconfirmed</span>
                      <span className="text-xs text-text-muted">Check YouTube</span>
                    </div>
                  )}
                </div>
                <div className="text-text-muted">
                  {item.resolved_at ? new Date(item.resolved_at).toLocaleString() : (item.created_at ? new Date(item.created_at).toLocaleString() : 'Unknown')}
                </div>
              </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
