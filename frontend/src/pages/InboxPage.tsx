import { useState, useEffect } from 'react';
import { formatDate, formatTime } from '../lib/format';
import { Search, RefreshCw, MessageCircle, CircleCheck, CircleAlert, Info, Video, X } from 'lucide-react';
import { Avatar, PageHeader } from '../components/ui';
import type { CommentItem } from '../lib/types';
import DraftPanel from '../DraftPanel';
import { ATTENTION_STATUSES, DRAFT_STATUSES, explain, statusStyle } from '../lib/decisions';
import './InboxPage.css';
import { SafeText } from '../components/SafeText';

export default function InboxPage({ appData }: { appData: any }) {
  const { comments, commentsLoading, commentsError, selectedComment, setSelectedComment, selectedVideo, session, selectVideo, loadComments, videos } = appData;
  
  const [activeTab, setActiveTab] = useState('all');
  const [search, setSearch] = useState('');
  // Time the comment list last finished loading, instead of a fixed "just now" label.
  // Recorded when a load finishes while this page is open (state adjusted during render).
  const [syncedAt, setSyncedAt] = useState<Date | null>(null);
  const [wasLoading, setWasLoading] = useState<boolean>(Boolean(commentsLoading));
  if (wasLoading !== Boolean(commentsLoading)) {
    setWasLoading(Boolean(commentsLoading));
    if (!commentsLoading && !commentsError) setSyncedAt(new Date());
  }

  // Initial load if no comments are loaded yet
  useEffect(() => {
    if (session?.connected && comments.length === 0 && !commentsLoading) {
      if (!selectedVideo) {
        selectVideo(null); // Triggers loading channel comments
      }
    }
  }, [session?.connected]);

  const filteredComments = comments.filter((c: any) => {
    if (search && !c.text.toLowerCase().includes(search.toLowerCase()) && !c.author_name.toLowerCase().includes(search.toLowerCase())) return false;
    if (activeTab === 'review' && !ATTENTION_STATUSES.includes(c.reply_status)) return false;
    if (activeTab === 'drafts' && !DRAFT_STATUSES.includes(c.reply_status)) return false;
    return true;
  });

  const reviewCount = comments.filter((c: any) => ATTENTION_STATUSES.includes(c.reply_status)).length;
  const draftCount = comments.filter((c: any) => DRAFT_STATUSES.includes(c.reply_status)).length;

  // Find the actual comment object
  const selectedCommentObj = comments.find((c: any) => c.comment_id === selectedComment);
  
  // The effective video context to pass to the workspace
  const effectiveVideoId = selectedCommentObj?.video_id || selectedVideo?.video_id;
  const effectiveVideo = videos?.find((v: any) => v.video_id === effectiveVideoId)
    || selectedVideo
    || (selectedCommentObj ? {
        video_id: selectedCommentObj.video_id,
        title: selectedCommentObj.video_title || 'Unknown Video',
        thumbnail_url: null,
        published_at: null
      } : null);

  // Auto-selection logic
  useEffect(() => {
    if (!commentsLoading && comments.length > 0) {
      // If the selected comment is no longer in the filtered list (or we haven't selected one)
      const isSelectedVisible = filteredComments.some((c: any) => c.comment_id === selectedComment);
      
      if (!selectedComment || !isSelectedVisible) {
        // Priority: Needs review -> Approved/Draft ready -> No draft -> Posted
        const reviewComment = filteredComments.find((c: any) => ATTENTION_STATUSES.includes(c.reply_status));
        const draftComment = filteredComments.find((c: any) => DRAFT_STATUSES.includes(c.reply_status));
        const noDraftComment = filteredComments.find((c: any) => !c.reply_status || c.reply_status === 'No draft');
        const bestComment = reviewComment || draftComment || noDraftComment || filteredComments[0];
        
        if (bestComment && bestComment.comment_id !== selectedComment) {
          setSelectedComment(bestComment.comment_id);
        }
      }
    }
  }, [comments, commentsLoading, activeTab, search, selectedComment, filteredComments, setSelectedComment]);

  return (
    <div className="page">
      <PageHeader
        title="Comment inbox"
        subtitle="Review and reply across all your videos."
        actions={
          <button
            onClick={() => loadComments?.(selectedVideo ? selectedVideo.video_id : null, null, false)}
            disabled={commentsLoading}
            title="Fetch the latest comments from YouTube for this channel"
            className="btn btn-primary btn-lg"
          >
            <RefreshCw className={commentsLoading ? 'animate-spin' : ''} aria-hidden="true" />
            Sync comments
          </button>
        }
        meta={commentsLoading ? 'Syncing…' : syncedAt ? `Last synced ${formatTime(syncedAt)}` : null}
      />

      <section className="inbox-grid">

        {/* Left column: comment browser */}
        <div className="card inbox-list-card">
          <div className="inbox-toolbar">
            <div className="flex min-w-0 flex-wrap gap-2 sm:flex-nowrap">
              <div className="relative min-w-[12rem] flex-1">
                <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" aria-hidden="true" />
                <input
                  type="text"
                  placeholder="Search comments..."
                  aria-label="Search comments"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="input pl-10"
                />
              </div>
              <button
                onClick={() => selectVideo(null)}
                className="btn inbox-video-filter"
                title={selectedVideo ? 'Clear the video filter and show all videos' : 'Showing all videos'}
              >
                <Video className="text-text-muted" aria-hidden="true" />
                <span className="min-w-0 truncate">{selectedVideo ? selectedVideo.title : 'All videos'}</span>
                {selectedVideo ? <span className="inline-flex items-center gap-1 text-brand"><X aria-hidden="true" />Clear</span> : null}
              </button>
              <button
                onClick={() => loadComments(selectedVideo ? selectedVideo.video_id : null, null, false)}
                disabled={commentsLoading}
                aria-label="Refresh comment list"
                title="Refresh the comment list"
                className="btn btn-icon"
              >
                <RefreshCw className={commentsLoading ? 'animate-spin' : ''} aria-hidden="true" />
              </button>
            </div>

            <div className="tabs mt-4" role="group" aria-label="Filter comments">
              {[
                { id: 'all', label: 'All', count: comments.length },
                { id: 'review', label: 'Needs review', count: reviewCount },
                { id: 'drafts', label: 'Drafts', count: draftCount }
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  aria-pressed={activeTab === tab.id}
                  className="tab"
                >
                  {tab.label}
                  <span className="tab-count">{tab.count}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="inbox-list">
            {commentsLoading ? (
              <div className="empty-state" role="status">Loading comments…</div>
            ) : commentsError ? (
              <div className="alert alert-error" role="alert">{commentsError}</div>
            ) : filteredComments.length === 0 ? (
              <div className="empty-state">
                <MessageCircle aria-hidden="true" />
                <p>{comments.length === 0 ? 'No comments available. Make sure a video is selected.' : 'No comments match this filter.'}</p>
              </div>
            ) : (
              filteredComments.map((c: CommentItem) => {
                const isSelected = selectedComment === c.comment_id;
                const videoTitle = selectedVideo?.title || c.video_title
                  || videos?.find((v: any) => v.video_id === c.video_id)?.title;
                const ReasonIcon = c.decision_code === 'eligible' ? CircleCheck
                  : ATTENTION_STATUSES.includes(c.reply_status) ? CircleAlert : Info;
                return (
                  <button
                    key={c.comment_id}
                    onClick={() => setSelectedComment(isSelected ? null : c.comment_id)}
                    aria-label={`Select comment by ${c.author_name}`}
                    aria-pressed={isSelected}
                    className={`inbox-comment ${isSelected ? 'is-selected' : ''}`}
                  >
                    <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2">
                      <div className="flex min-w-0 flex-[1_1_auto] items-center gap-3">
                        <Avatar name={c.author_name} src={c.author_thumbnail_url} />
                        <span className="min-w-0 truncate text-base font-bold text-text-main">{c.author_name}</span>
                        <span className="shrink-0 text-sm text-text-muted">{formatDate(c.published_at)}</span>
                      </div>
                      <span className={statusStyle(c.reply_status)}
                        title={c.decision_code ? explain(c.decision_code) : undefined}>
                        {c.reply_status || 'No draft'}
                      </span>
                    </div>
                    <p className="mt-3 text-base text-text-main [overflow-wrap:anywhere] line-clamp-3"><SafeText text={c.text} /></p>
                    {c.decision_code && c.reply_status !== 'Posted' ? (
                      <p className="mt-2 flex items-start gap-2 text-[0.9375rem] text-text-dim">
                        <ReasonIcon className={`mt-0.5 h-4 w-4 shrink-0 ${c.decision_code === 'eligible' ? 'text-brand' : ATTENTION_STATUSES.includes(c.reply_status) ? 'text-warning' : 'text-text-muted'}`} aria-hidden="true" />
                        <span className="min-w-0">{explain(c.decision_code)}</span>
                      </p>
                    ) : null}
                    {videoTitle ? <div className="mt-2 flex min-w-0 items-center gap-2 text-sm text-text-muted">
                      <Video className="h-4 w-4 shrink-0" aria-hidden="true" />
                      <span className="truncate">{videoTitle}</span>
                    </div> : null}
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Right column: reply workspace */}
        <div className="flex min-w-0 flex-col">
           {selectedComment && effectiveVideo ? (
             <div className="card flex min-w-0 flex-col overflow-hidden">
               {/* Video context */}
               <section aria-label="Selected video" className="mx-4 flex min-w-0 items-center gap-4 border-b border-[var(--line)] py-4 md:mx-6 md:pt-6">
                 {effectiveVideo.thumbnail_url ? (
                   <img src={effectiveVideo.thumbnail_url} alt="" className="h-14 w-24 shrink-0 rounded-xl border border-[var(--line)] object-cover" />
                 ) : (
                   <span className="inbox-video-thumb" aria-hidden="true"><Video /></span>
                 )}
                 <div className="flex min-w-0 flex-col">
                   <span className="truncate text-lg font-bold text-text-main">{effectiveVideo.title}</span>
                   {effectiveVideo.published_at && (
                     <span className="text-[0.9375rem] text-text-muted">Published {formatDate(effectiveVideo.published_at)}</span>
                   )}
                 </div>
               </section>

               <div className="min-h-0 min-w-0 flex-1 styled-draft-panel overflow-y-auto p-4 md:p-6">
                 {session?.csrf_token ? (
                   <DraftPanel key={`${effectiveVideo.video_id}:${selectedComment}`} videoId={effectiveVideo.video_id} commentId={selectedComment} csrf={session.csrf_token}
                     simulated={Boolean(session.simulation)} onSaved={() => void appData.refreshCommentStatuses?.()} />
                 ) : (
                   <div className="empty-state">Authentication required to view drafts.</div>
                 )}
               </div>
             </div>
           ) : (
             <div className="card empty-state min-h-[22rem] border-dashed">
               <MessageCircle aria-hidden="true" />
               <h3 className="font-semibold text-text-main">No comment selected</h3>
               <p className="max-w-[16rem] text-[0.9375rem]">Select a comment from the inbox to open the reply workspace.</p>
             </div>
           )}
        </div>
      </section>
    </div>
  );
}
