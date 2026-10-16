import { useState, useEffect } from 'react';
import { Search, RefreshCw, MessageCircle } from 'lucide-react';
import type { CommentItem } from '../lib/types';
import DraftPanel from '../DraftPanel';

export default function InboxPage({ appData }: { appData: any }) {
  const { comments, commentsLoading, commentsError, selectedComment, setSelectedComment, selectedVideo, session, selectVideo, loadComments, videos } = appData;
  
  const [activeTab, setActiveTab] = useState('all');
  const [search, setSearch] = useState('');

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
    if (activeTab === 'review' && c.reply_status !== 'Needs review') return false;
    if (activeTab === 'drafts' && !['Draft ready', 'Approved'].includes(c.reply_status)) return false;
    return true;
  });

  const reviewCount = comments.filter((c: any) => c.reply_status === 'Needs review').length;
  const draftCount = comments.filter((c: any) => ['Draft ready', 'Approved'].includes(c.reply_status)).length;

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
        const reviewComment = filteredComments.find((c: any) => c.reply_status === 'Needs review');
        const draftComment = filteredComments.find((c: any) => ['Draft ready', 'Approved'].includes(c.reply_status));
        const noDraftComment = filteredComments.find((c: any) => !['Posted', 'Needs review', 'Draft ready', 'Approved'].includes(c.reply_status));
        const bestComment = reviewComment || draftComment || noDraftComment || filteredComments[0];
        
        if (bestComment && bestComment.comment_id !== selectedComment) {
          setSelectedComment(bestComment.comment_id);
        }
      }
    }
  }, [comments, commentsLoading, activeTab, search, selectedComment, filteredComments, setSelectedComment]);

  return (
    <div className="flex flex-col h-full gap-6 animate-in fade-in duration-300">
      <header className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight mb-1">Comment inbox</h1>
          <p className="text-text-muted text-sm">Review and reply across all your videos.</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <button 
            onClick={() => loadComments?.(selectedVideo ? selectedVideo.video_id : null, null, false)}
            disabled={commentsLoading}
            title="Fetch the latest comments from YouTube for this channel"
            className="flex items-center gap-2 px-4 py-2 rounded-full border border-dark-600 bg-dark-800/80 hover:bg-dark-700 text-sm font-medium transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${commentsLoading ? 'animate-spin' : ''}`} />
            Sync comments
          </button>
          <span className="text-xs text-text-muted">Last synced Just now</span>
        </div>
      </header>

      <section className="flex-1 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,500px)] xl:grid-cols-[minmax(0,1fr)_minmax(0,600px)] gap-6 min-h-0">
        
        {/* Left Column: Comment Browser */}
        <div className="flex flex-col rounded-2xl border border-dark-600 bg-[#0a0a0c]/60 backdrop-blur-sm overflow-hidden h-[calc(100vh-180px)] md:h-auto md:min-h-[calc(100vh-180px)]">
          <div className="p-4 border-b border-dark-600 space-y-4">
            <div className="flex gap-4">
              <div className="relative flex-1">
                <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
                <input 
                  type="text" 
                  placeholder="Search comments..." 
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="w-full pl-9 pr-4 py-2 bg-dark-900 border border-dark-600 rounded-lg text-sm text-text-main focus:outline-none focus:border-brand"
                />
              </div>
              <button 
                onClick={() => selectVideo(null)}
                className="flex items-center gap-2 px-3 py-2 bg-dark-900 border border-dark-600 hover:bg-dark-800 rounded-lg text-sm whitespace-nowrap transition-colors"
                title={selectedVideo ? "Click to clear filter and show all videos" : "Showing all videos"}
              >
                <VideoIcon className="w-4 h-4 text-brand" />
                {selectedVideo ? selectedVideo.title.substring(0, 20) + '... (Click to clear)' : 'All videos'}
              </button>
              <button 
                onClick={() => loadComments(selectedVideo ? selectedVideo.video_id : null, null, false)}
                disabled={commentsLoading}
                aria-label="Refresh comment list"
                title="Refresh the comment list"
                className="flex items-center gap-2 px-3 py-2 bg-dark-900 border border-dark-600 hover:bg-dark-800 rounded-lg text-sm whitespace-nowrap transition-colors disabled:opacity-50"
              >
                <RefreshCw className={`w-4 h-4 ${commentsLoading ? 'animate-spin' : ''}`} />
              </button>
            </div>
            
            <div className="flex gap-2">
              {[
                { id: 'all', label: 'All', count: comments.length },
                { id: 'review', label: 'Needs review', count: reviewCount },
                { id: 'drafts', label: 'Drafts', count: draftCount }
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex items-center gap-2 px-4 py-1.5 rounded-full text-sm font-medium transition-colors ${
                    activeTab === tab.id ? 'bg-brand/20 text-brand-light border border-brand/30' : 'text-text-muted hover:text-text-main hover:bg-dark-700'
                  }`}
                >
                  {tab.label}
                  <span className={`px-1.5 py-0.5 rounded-md text-xs ${activeTab === tab.id ? 'bg-brand/30 text-brand-light' : 'bg-dark-600'}`}>
                    {tab.count}
                  </span>
                </button>
              ))}
            </div>
          </div>
          
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {commentsLoading ? (
              <div className="text-center text-text-muted py-8 text-sm">Loading comments...</div>
            ) : commentsError ? (
              <div className="text-center text-danger py-8 text-sm" role="alert">{commentsError}</div>
            ) : filteredComments.length === 0 ? (
              <div className="text-center text-text-muted py-8 text-sm">
                No comments available. Make sure a video is selected.
                <br/><br/>
                
              </div>
            ) : (
              filteredComments.map((c: CommentItem) => {
                const isSelected = selectedComment === c.comment_id;
                return (
                  <button 
                    key={c.comment_id}
                    onClick={() => setSelectedComment(isSelected ? null : c.comment_id)}
                    aria-label={`Select comment by ${c.author_name}`}
                    aria-pressed={isSelected}
                    className={`w-full text-left flex flex-col p-4 rounded-xl border transition-colors ${
                      isSelected ? 'border-brand bg-brand/5 shadow-[0_0_15px_rgba(156,107,255,0.1)]' : 'border-dark-600 bg-dark-900/50 hover:bg-dark-700'
                    }`}
                  >
                    <div className="flex gap-4">
                      {selectedVideo?.thumbnail_url && (
                        <img src={selectedVideo.thumbnail_url} alt="" className="w-24 h-14 object-cover rounded border border-dark-600 hidden sm:block" />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between mb-2">
                          <div className="flex items-center gap-2">
                            {c.author_thumbnail_url ? (
                              <img src={c.author_thumbnail_url} alt="" className="w-6 h-6 rounded-full" />
                            ) : (
                              <div className="w-6 h-6 rounded-full bg-dark-600 flex items-center justify-center text-xs">{c.author_name.charAt(0)}</div>
                            )}
                            <span className="font-medium text-sm">{c.author_name}</span>
                            <span className="text-xs text-text-muted">{c.published_at ? new Date(c.published_at).toLocaleDateString() : ''}</span>
                          </div>
                          <span className={`px-2 py-0.5 rounded-full text-xs font-medium border ${
                            c.reply_status === 'Posted' ? 'bg-success/20 text-success border-success/30' :
                            c.reply_status === 'Failed' ? 'bg-danger/20 text-danger border-danger/30' :
                            c.reply_status === 'Unconfirmed' ? 'bg-warning/20 text-warning border-warning/30' :
                            c.reply_status === 'Needs review' ? 'bg-brand/20 text-brand-light border-brand/30' :
                            c.reply_status === 'Approved' ? 'bg-success/10 text-success border-success/20' :
                            c.reply_status === 'Draft ready' ? 'bg-dark-600 text-text-main border-dark-500' :
                            'bg-dark-600 text-text-muted border-dark-500'
                          }`}>
                            {c.reply_status || 'No draft'}
                          </span>
                        </div>
                        <p className="text-sm text-text-dim line-clamp-2 mb-2">{c.text}</p>
                        <div className="text-xs text-text-muted truncate">
                          {selectedVideo?.title || c.video_title}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Right Column: Draft Workspace */}
        <div className="flex min-w-0 flex-col h-[calc(100vh-180px)] md:h-auto md:min-h-[calc(100vh-180px)] overflow-hidden">
           {selectedComment && effectiveVideo ? (
             <div className="flex min-h-0 min-w-0 flex-col h-full rounded-2xl border border-dark-600 bg-[#0a0a0c]/60 backdrop-blur-sm overflow-hidden">
               {/* Video Context */}
               <div className="flex items-center gap-3 p-4 border-b border-dark-600 bg-dark-900/30">
                 {effectiveVideo.thumbnail_url ? (
                   <img src={effectiveVideo.thumbnail_url} alt="" className="w-12 h-7 object-cover rounded border border-dark-600" />
                 ) : (
                   <div className="w-12 h-7 rounded bg-dark-600" />
                 )}
                 <div className="flex flex-col">
                   <span className="text-sm font-medium truncate">{effectiveVideo.title}</span>
                   {effectiveVideo.published_at && (
                     <span className="text-xs text-text-muted">Published {new Date(effectiveVideo.published_at).toLocaleDateString()}</span>
                   )}
                 </div>
               </div>
               
               <div className="min-h-0 min-w-0 flex-1 styled-draft-panel overflow-y-auto p-4 md:p-6">
                 {session?.csrf_token ? (
                   <DraftPanel key={`${effectiveVideo.video_id}:${selectedComment}`} videoId={effectiveVideo.video_id} commentId={selectedComment} csrf={session.csrf_token} />
                 ) : (
                   <div className="text-center text-text-muted py-8 text-sm">Authentication required to view drafts.</div>
                 )}
               </div>
             </div>
           ) : (
             <div className="flex-1 flex flex-col items-center justify-center rounded-2xl border border-dark-600 bg-[#0a0a0c]/60 border-dashed p-6 text-center">
               <MessageCircle className="w-10 h-10 text-dark-500 mb-3" />
               <h3 className="text-text-main font-medium mb-1">No comment selected</h3>
               <p className="text-text-muted text-sm max-w-[200px]">Select a comment from the inbox to open the draft workspace.</p>
             </div>
           )}
        </div>
      </section>
    </div>
  );
}

// Inline VideoIcon component to avoid another import
function VideoIcon(props: any) {
  return (
    <svg {...props} xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/>
      <rect x="2" y="6" width="14" height="12" rx="2"/>
    </svg>
  );
}
