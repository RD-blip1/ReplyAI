import { useEffect, useState } from 'react';
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  MessageCircle,
  Play,
  RefreshCcw,
} from 'lucide-react';
import { useAppData } from '../hooks/useAppData';
import type { CommentItem, VideoItem } from '../lib/types';
import DraftPanel from '../DraftPanel';
import './VideosPage.css';

type VideosPageData = Pick<
  ReturnType<typeof useAppData>,
  | 'session'
  | 'videos'
  | 'videosLoading'
  | 'videosError'
  | 'comments'
  | 'commentsLoading'
  | 'commentsError'
  | 'selectedVideo'
  | 'selectVideo'
  | 'selectedComment'
  | 'setSelectedComment'
  | 'loadVideoPage'
>;

interface VideosPageProps {
  appData: VideosPageData;
}

type CommentFilter = 'all' | 'review' | 'drafts';

function formatDate(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? null
    : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function statusClasses(status: string | null | undefined): string {
  switch ((status ?? '').toLowerCase()) {
    case 'needs review':
      return 'video-status video-status-review';
    case 'draft ready':
    case 'approved':
      return 'video-status video-status-draft';
    case 'posted':
    case 'replied':
      return 'video-status video-status-posted';
    default:
      return 'video-status';
  }
}

function matchesFilter(comment: CommentItem, filter: CommentFilter): boolean {
  if (filter === 'review') return comment.reply_status === 'Needs review';
  if (filter === 'drafts') return ['Draft ready', 'Approved'].includes(comment.reply_status);
  return true;
}

export default function VideosPage({ appData }: VideosPageProps) {
  const {
    session,
    videos,
    videosLoading,
    videosError,
    comments,
    commentsLoading,
    commentsError,
    selectedVideo,
    selectVideo,
    selectedComment,
    setSelectedComment,
  } = appData;
  const [commentFilter, setCommentFilter] = useState<CommentFilter>('all');
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);

  useEffect(() => {
    if (videos.length > 0 && !selectedVideo) selectVideo(videos[0]);
  }, [videos, selectedVideo, selectVideo]);

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const selectedIndex = selectedVideo
    ? videos.findIndex((video) => video.video_id === selectedVideo.video_id)
    : -1;
  const carouselIndex = selectedIndex >= 0 ? selectedIndex : 0;
  const canNavigate = videos.length > 0 && selectedIndex >= 0;
  const visibleComments = comments.filter((comment) => matchesFilter(comment, commentFilter));
  const reviewCount = comments.filter((comment) => comment.reply_status === 'Needs review').length;
  const draftCount = comments.filter((comment) => ['Draft ready', 'Approved'].includes(comment.reply_status)).length;

  const selectAt = (index: number) => {
    const video = videos[index];
    if (video) selectVideo(video);
  };
  const selectPrevious = () => selectAt(carouselIndex - 1);
  const selectNext = () => selectAt(carouselIndex + 1);

  const handleCarouselKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    const target = event.target;
    if (
      target instanceof HTMLElement
      && target.matches('input, textarea, select, [contenteditable="true"]')
    ) return;

    if (event.key === 'ArrowLeft' && carouselIndex > 0) {
      event.preventDefault();
      selectPrevious();
    } else if (event.key === 'ArrowRight' && carouselIndex < videos.length - 1) {
      event.preventDefault();
      selectNext();
    } else if (event.key === 'Home' && videos.length > 0) {
      event.preventDefault();
      selectAt(0);
    } else if (event.key === 'End' && videos.length > 0) {
      event.preventDefault();
      selectAt(videos.length - 1);
    }
  };

  const syncVideos = () => {
    const controller = new AbortController();
    void appData.loadVideoPage(null, false, controller.signal);
  };

  return (
    <div className="videos-page flex flex-col gap-5 animate-in fade-in duration-300">
      <header className="videos-hero flex items-center justify-between gap-4">
        <div>
          <p className="videos-eyebrow">CHANNEL LIBRARY</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight text-white sm:text-[2rem]">Your videos</h1>
          <p className="mt-1 text-sm text-text-muted">Pick a video. Keep the conversation going.</p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <button
            type="button"
            onClick={syncVideos}
            disabled={!session?.connected || videosLoading}
            className="videos-button-primary flex min-h-10 items-center gap-2 rounded-full px-4 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCcw className={`h-4 w-4 ${videosLoading ? 'animate-spin' : ''}`} />
            {videosLoading ? 'Syncing…' : 'Sync videos'}
          </button>
          <span className="text-xs text-text-dim">
            {session?.connected ? 'Sync your latest uploads' : 'Connect YouTube to load videos'}
          </span>
        </div>
      </header>

      {videosError && <p className="videos-error" role="alert">{videosError}</p>}

      <section
        className="videos-carousel"
        aria-label="Your videos"
        aria-roledescription="carousel"
        tabIndex={0}
        onKeyDown={handleCarouselKeyDown}
      >
        <div className="videos-carousel-track" aria-label="Video selection">
          {videos.map((video: VideoItem, index) => {
            const offset = index - carouselIndex;
            const distance = Math.abs(offset);
            if (distance > 3) return null;

            const isSelected = video.video_id === selectedVideo?.video_id;
            const step = viewportWidth <= 640 ? 34 : viewportWidth < 1024 ? 21 : 17;
            const scale = isSelected ? 1 : Math.max(0.72, 1 - distance * 0.11);
            const rotateY = offset === 0 ? 0 : offset < 0 ? 24 : -24;
            const imageDate = formatDate(video.published_at);

            return (
              <button
                key={video.video_id}
                type="button"
                onClick={() => selectVideo(video)}
                aria-label={`Select video: ${video.title}`}
                aria-pressed={isSelected}
                aria-roledescription="slide"
                className={`video-carousel-card ${isSelected ? 'is-selected' : ''}`}
                style={{
                  left: `calc(50% + ${offset * step}vw)`,
                  transform: `translate(-50%, -50%) translateZ(${isSelected ? 50 : -distance * 45}px) rotateY(${rotateY}deg) scale(${scale})`,
                  zIndex: 20 - distance,
                  opacity: Math.max(0.7, 1 - distance * 0.12),
                }}
              >
                {video.thumbnail_url ? (
                  <img src={video.thumbnail_url} alt="" className="h-full w-full object-cover" />
                ) : (
                  <span className="video-thumbnail-fallback" aria-hidden="true"><Play /></span>
                )}
                <span className="video-carousel-shade" aria-hidden="true" />
                <span className="video-card-copy">
                  <span className="line-clamp-2 text-left text-sm font-semibold leading-snug text-white sm:text-base">
                    {video.title}
                  </span>
                  {isSelected && imageDate && (
                    <span className="mt-1.5 flex items-center gap-1.5 text-xs text-white/70">
                      <Check className="h-3.5 w-3.5 text-brand" />
                      Published {imageDate}
                    </span>
                  )}
                </span>
                {isSelected && <span className="video-card-indicator" aria-hidden="true" />}
              </button>
            );
          })}

          {videosLoading && videos.length === 0 && (
            <p className="text-sm text-text-muted" role="status">Loading your videos…</p>
          )}
          {!videosLoading && videos.length === 0 && (
            <p className="px-4 text-center text-sm text-text-muted" role="status">
              {session?.connected ? 'No videos found for this channel.' : 'Connect a YouTube channel to see your videos.'}
            </p>
          )}
        </div>

        <div className="videos-carousel-controls">
          <button
            type="button"
            onClick={selectPrevious}
            aria-label="Previous video"
            disabled={!canNavigate || carouselIndex === 0}
            className="video-round-control"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <p className="min-w-14 text-center font-mono text-xs tracking-[0.16em] text-text-muted" aria-live="polite">
            {videos.length > 0
              ? `${String(carouselIndex + 1).padStart(2, '0')} / ${String(videos.length).padStart(2, '0')}`
              : '— / —'}
          </p>
          <button
            type="button"
            onClick={selectNext}
            aria-label="Next video"
            disabled={!canNavigate || carouselIndex >= videos.length - 1}
            className="video-round-control"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
          {selectedVideo?.watch_url && (
            <a
              href={selectedVideo.watch_url}
              target="_blank"
              rel="noreferrer"
              aria-label="Open selected video on YouTube"
              className="video-round-control ml-1"
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
        </div>
        <p className="sr-only" aria-live="polite">
          {selectedVideo ? `Selected video ${carouselIndex + 1} of ${videos.length}: ${selectedVideo.title}` : ''}
        </p>
      </section>

      {selectedVideo && (
        <section className="videos-workspace grid min-h-0 grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
          <div className="videos-panel flex min-h-[420px] max-h-[720px] flex-col overflow-hidden">
            <div className="videos-panel-heading flex items-center justify-between gap-3 px-5 py-4 sm:px-6">
              <div>
                <p className="videos-eyebrow">CONVERSATION</p>
                <h2 className="mt-1 text-lg font-semibold text-white">Comments</h2>
              </div>
              <div className="videos-filter-label" title={selectedVideo.title}>
                <Play className="h-3.5 w-3.5 shrink-0 text-brand" />
                <span className="max-w-28 truncate sm:max-w-40">{selectedVideo.title}</span>
                <ChevronDown className="h-3.5 w-3.5 shrink-0 text-text-dim" />
              </div>
            </div>

            <div className="videos-comment-tabs" role="group" aria-label="Filter comments">
              {([
                { id: 'all', label: 'All', count: comments.length },
                { id: 'review', label: 'Needs review', count: reviewCount },
                { id: 'drafts', label: 'Drafts', count: draftCount },
              ] as const).map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  aria-pressed={commentFilter === tab.id}
                  onClick={() => setCommentFilter(tab.id)}
                  className={`videos-comment-tab ${commentFilter === tab.id ? 'is-active' : ''}`}
                >
                  {tab.label}<span>{tab.count}</span>
                </button>
              ))}
            </div>

            <div className="videos-comments-list">
              {commentsLoading && comments.length === 0 ? (
                <p className="py-8 text-center text-sm text-text-muted" role="status">Loading comments…</p>
              ) : commentsError ? (
                <p className="videos-error" role="alert">{commentsError}</p>
              ) : visibleComments.length === 0 ? (
                <div className="px-5 py-10 text-center">
                  <MessageCircle className="mx-auto h-6 w-6 text-text-dim" />
                  <p className="mt-2 text-sm text-text-muted">
                    {comments.length === 0 ? 'No comments to display for this video.' : 'No comments match this filter.'}
                  </p>
                </div>
              ) : visibleComments.map((comment) => {
                const isSelected = comment.comment_id === selectedComment;
                return (
                  <button
                    key={comment.comment_id}
                    type="button"
                    onClick={() => setSelectedComment(isSelected ? null : comment.comment_id)}
                    aria-label={`Select comment by ${comment.author_name}`}
                    aria-pressed={isSelected}
                    className={`videos-comment-row ${isSelected ? 'is-selected' : ''}`}
                  >
                    {selectedVideo.thumbnail_url ? (
                      <img src={selectedVideo.thumbnail_url} alt="" className="videos-comment-video-thumb" />
                    ) : (
                      <span className="videos-comment-video-thumb videos-thumb-fallback"><Play className="h-4 w-4" /></span>
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="flex min-w-0 items-center gap-2">
                        {comment.author_thumbnail_url ? (
                          <img src={comment.author_thumbnail_url} alt="" className="videos-comment-avatar" />
                        ) : (
                          <span className="videos-comment-avatar videos-avatar-fallback" aria-hidden="true">
                            {comment.author_name.charAt(0).toUpperCase()}
                          </span>
                        )}
                        <span className="min-w-0 flex-1">
                          <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                            <span className="truncate text-sm font-medium text-white">{comment.author_name}</span>
                            {formatDate(comment.published_at) && (
                              <span className="text-[11px] text-text-dim">{formatDate(comment.published_at)}</span>
                            )}
                          </span>
                          <span className="mt-1 block line-clamp-2 text-left text-xs leading-relaxed text-text-muted sm:text-sm">
                            {comment.text}
                          </span>
                        </span>
                      </span>
                    </span>
                    <span className={statusClasses(comment.reply_status)}>
                      {comment.reply_status || 'Status unavailable'}
                    </span>
                  </button>
                );
              })}
            </div>
            <span className="sr-only" aria-live="polite">
              {selectedComment ? 'A comment is selected for the reply workspace.' : 'No comment selected.'}
            </span>
          </div>

          <div className="videos-panel videos-draft-panel styled-draft-panel min-h-[420px] overflow-hidden p-4 sm:p-5">
            {selectedComment && session?.csrf_token ? (
              <DraftPanel
                key={`${selectedVideo.video_id}:${selectedComment}`}
                videoId={selectedVideo.video_id}
                commentId={selectedComment}
                csrf={session.csrf_token}
              />
            ) : selectedComment ? (
              <div className="flex min-h-[380px] items-center justify-center text-center text-sm text-text-muted">
                Authentication required to view drafts.
              </div>
            ) : (
              <div className="flex min-h-[380px] flex-col items-center justify-center px-6 text-center">
                <span className="videos-empty-icon"><MessageCircle className="h-5 w-5" /></span>
                <h2 className="mt-4 text-base font-semibold text-white">Reply draft</h2>
                <p className="mt-1 max-w-xs text-sm leading-relaxed text-text-muted">
                  Select a comment to open its saved reply, review status, and publishing controls.
                </p>
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
