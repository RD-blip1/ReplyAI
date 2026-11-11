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
import { ATTENTION_STATUSES, DRAFT_STATUSES, statusStyle } from '../lib/decisions';
import { Avatar, PageHeader } from '../components/ui';
import type { CommentItem, VideoItem } from '../lib/types';
import DraftPanel from '../DraftPanel';
import { formatDate } from '../lib/format';
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
> & Partial<Pick<ReturnType<typeof useAppData>, 'refreshCommentStatuses'>>;

interface VideosPageProps {
  appData: VideosPageData;
}

type CommentFilter = 'all' | 'review' | 'drafts';


function matchesFilter(comment: CommentItem, filter: CommentFilter): boolean {
  if (filter === 'review') return ATTENTION_STATUSES.includes(comment.reply_status);
  if (filter === 'drafts') return DRAFT_STATUSES.includes(comment.reply_status);
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
  const reviewCount = comments.filter((comment) => ATTENTION_STATUSES.includes(comment.reply_status)).length;
  const draftCount = comments.filter((comment) => DRAFT_STATUSES.includes(comment.reply_status)).length;

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

  // Carousel geometry in px, so cards are positioned with transforms only.
  const narrow = viewportWidth <= 640;
  const mainWidth = narrow ? Math.min(viewportWidth * 0.8, 400) : Math.min(viewportWidth * 0.41, 590);
  const sideWidth = narrow ? mainWidth * 0.62 : Math.min(viewportWidth * 0.21, 300);
  const sideGap = narrow ? 12 : Math.min(viewportWidth * 0.028, 40);
  const step = mainWidth / 2 + sideGap + sideWidth / 2;
  const trackHeight = Math.round((mainWidth - 28) * 9 / 16 + (narrow ? 96 : 116));

  return (
    <div className="videos-page page">
      <PageHeader
        eyebrow="Channel library"
        title="Your videos"
        subtitle="Pick a video. Keep the conversation going."
        actions={
          <button
            type="button"
            onClick={syncVideos}
            disabled={!session?.connected || videosLoading}
            className="btn btn-primary btn-lg"
          >
            <RefreshCcw className={videosLoading ? 'animate-spin' : ''} aria-hidden="true" />
            {videosLoading ? 'Syncing…' : 'Sync videos'}
          </button>
        }
        meta={session?.connected ? 'Sync your latest uploads' : 'Connect YouTube to load videos'}
      />

      {videosError && <p className="alert alert-error" role="alert">{videosError}</p>}

      <section
        className="videos-carousel"
        aria-label="Your videos"
        aria-roledescription="carousel"
        tabIndex={0}
        onKeyDown={handleCarouselKeyDown}
      >
        <div className="videos-carousel-track" aria-label="Video selection" style={{ height: trackHeight }}>
          {videos.map((video: VideoItem, index) => {
            const offset = index - carouselIndex;
            const distance = Math.abs(offset);
            if (distance > 3) return null;

            const isSelected = video.video_id === selectedVideo?.video_id;
            const width = isSelected ? mainWidth : sideWidth;
            const x = offset === 0 ? 0 : Math.sign(offset) * (step + (distance - 1) * (sideWidth + sideGap));
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
                  width,
                  transform: `translate(calc(-50% + ${x}px), -50%)`,
                  zIndex: 20 - distance,
                  opacity: isSelected ? 1 : Math.max(0.35, 0.78 - (distance - 1) * 0.3),
                }}
              >
                <span className="video-thumb">
                  {video.thumbnail_url ? (
                    <img src={video.thumbnail_url} alt="" />
                  ) : (
                    <span className="video-thumbnail-fallback" aria-hidden="true">
                      <span className="video-play"><Play /></span>
                    </span>
                  )}
                </span>
                <span className="video-card-copy">
                  <span className="video-card-title">{video.title}</span>
                  {isSelected && imageDate && (
                    <span className="video-card-date">
                      <Check aria-hidden="true" />
                      Published {imageDate}
                    </span>
                  )}
                </span>
              </button>
            );
          })}

          {videosLoading && videos.length === 0 && (
            <p className="text-[0.9375rem] text-text-muted" role="status">Loading your videos…</p>
          )}
          {!videosLoading && videos.length === 0 && (
            <p className="px-4 text-center text-[0.9375rem] text-text-muted" role="status">
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
            <ChevronLeft aria-hidden="true" />
          </button>
          <p className="video-counter" aria-live="polite">
            {videos.length > 0
              ? <><strong>{String(carouselIndex + 1).padStart(2, '0')}</strong> / {String(videos.length).padStart(2, '0')}</>
              : '— / —'}
          </p>
          <button
            type="button"
            onClick={selectNext}
            aria-label="Next video"
            disabled={!canNavigate || carouselIndex >= videos.length - 1}
            className="video-round-control"
          >
            <ChevronRight aria-hidden="true" />
          </button>
          {selectedVideo?.watch_url && (
            <a
              href={selectedVideo.watch_url}
              target="_blank"
              rel="noreferrer"
              aria-label="Open selected video on YouTube"
              className="video-round-control"
            >
              <ExternalLink aria-hidden="true" />
            </a>
          )}
        </div>
        <p className="sr-only" aria-live="polite">
          {selectedVideo ? `Selected video ${carouselIndex + 1} of ${videos.length}: ${selectedVideo.title}` : ''}
        </p>
      </section>

      {selectedVideo && (
        <section className="videos-workspace" aria-label="Comments and reply workspace">
          <div className="card videos-panel videos-comments-panel">
            <div className="videos-panel-heading">
              <div className="min-w-0">
                <p className="eyebrow">Conversation</p>
                <h2 className="mt-1 text-2xl font-bold tracking-tight text-text-main">Comments</h2>
              </div>
              <div className="videos-filter-label" title={selectedVideo.title}>
                <Play aria-hidden="true" />
                <span className="min-w-0 truncate">{selectedVideo.title}</span>
                <ChevronDown aria-hidden="true" />
              </div>
            </div>

            <div className="videos-comment-tabs tabs" role="group" aria-label="Filter comments">
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
                  className="tab"
                >
                  {tab.label}<span className="tab-count">{tab.count}</span>
                </button>
              ))}
            </div>

            <div className="videos-comments-list">
              {commentsLoading && comments.length === 0 ? (
                <p className="py-8 text-center text-[0.9375rem] text-text-muted" role="status">Loading comments…</p>
              ) : commentsError ? (
                <p className="alert alert-error videos-error" role="alert">{commentsError}</p>
              ) : visibleComments.length === 0 ? (
                <div className="empty-state">
                  <MessageCircle aria-hidden="true" />
                  <p className="text-[0.9375rem]">
                    {comments.length === 0 ? 'No comments to display for this video.' : 'No comments match this filter.'}
                  </p>
                </div>
              ) : visibleComments.map((comment) => {
                const isSelected = comment.comment_id === selectedComment;
                const date = formatDate(comment.published_at);
                return (
                  <button
                    key={comment.comment_id}
                    type="button"
                    onClick={() => setSelectedComment(isSelected ? null : comment.comment_id)}
                    aria-label={`Select comment by ${comment.author_name}`}
                    aria-pressed={isSelected}
                    className={`videos-comment-row ${isSelected ? 'is-selected' : ''}`}
                  >
                    <Avatar name={comment.author_name} src={comment.author_thumbnail_url} />
                    <span className="min-w-0 flex-1">
                      <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span className="videos-comment-author">{comment.author_name}</span>
                        {date && <span className="text-sm text-text-muted">{date}</span>}
                      </span>
                      <span className="videos-comment-text">{comment.text}</span>
                    </span>
                    <span className={statusStyle(comment.reply_status)}>
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

          <div className="card videos-panel videos-draft-panel">
            {selectedComment && session?.csrf_token ? (
              <DraftPanel
                key={`${selectedVideo.video_id}:${selectedComment}`}
                videoId={selectedVideo.video_id}
                commentId={selectedComment}
                csrf={session.csrf_token}
                simulated={Boolean(session.simulation)}
                onSaved={() => void appData.refreshCommentStatuses?.()}
              />
            ) : selectedComment ? (
              <div className="empty-state min-h-[22rem]">
                Authentication required to view drafts.
              </div>
            ) : (
              <div className="empty-state min-h-[22rem]">
                <MessageCircle aria-hidden="true" />
                <h2 className="text-lg font-semibold text-text-main">Reply workspace</h2>
                <p className="max-w-xs text-[0.9375rem] leading-relaxed">
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
