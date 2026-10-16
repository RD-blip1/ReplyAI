import { useState, useEffect, useRef } from 'react';
import { apiRequest, ApiError } from '../lib/api';
import type { ConnectionStatus, VideoItem, CommentItem, Page } from '../lib/types';

export function useAppData() {
  const [session, setSession] = useState<ConnectionStatus | null>(null)
  const [sessionLoading, setSessionLoading] = useState(true)
  const [sessionError, setSessionError] = useState<string | null>(null)
  
  const [videos, setVideos] = useState<VideoItem[]>([])
  const [videosLoading, setVideosLoading] = useState(false)
  const [videosError, setVideosError] = useState<string | null>(null)
  const [nextVideoPage, setNextVideoPage] = useState<string | null>(null)
  const [selectedVideo, setSelectedVideo] = useState<VideoItem | null>(null)
  
  const [selectedComment, setSelectedComment] = useState<string | null>(null)
  const [comments, setComments] = useState<CommentItem[]>([])
  const [commentsLoading, setCommentsLoading] = useState(false)
  const [commentsError, setCommentsError] = useState<string | null>(null)
  const [nextCommentPage, setNextCommentPage] = useState<string | null>(null)
  
  const videoController = useRef<AbortController | null>(null)
  const videoRequestSequence = useRef(0)
  const commentController = useRef<AbortController | null>(null)
  const commentRequestSequence = useRef(0)
  const selectedVideoId = useRef<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    void apiRequest<ConnectionStatus>('/api/session', { signal: controller.signal })
      .then((result) => {
        setSession(result)
        setSessionError(null)
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setSessionError(error instanceof Error ? error.message : 'ReplyAI could not be reached.')
      })
      .finally(() => setSessionLoading(false))

    return () => controller.abort()
  }, [])

  async function loadVideoPage(pageToken: string | null, append: boolean, signal: AbortSignal) {
    const requestSequence = ++videoRequestSequence.current
    setVideosLoading(true)
    setVideosError(null)
    const query = pageToken ? `?page_token=${encodeURIComponent(pageToken)}` : ''

    try {
      const page = await apiRequest<Page<VideoItem>>(`/api/youtube/videos${query}`, { signal })
      if (requestSequence !== videoRequestSequence.current) return
      setVideos((current) => (append ? [...current, ...page.items] : page.items))
      setNextVideoPage(page.next_page_token)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (requestSequence !== videoRequestSequence.current) return
      const apiError = error instanceof ApiError ? error : null
      setVideosError(error instanceof Error ? error.message : 'Videos could not be loaded.')
      if (apiError?.code === 'reconnect_required') {
        setSession((current) => current?.channel ? { ...current, channel: { ...current.channel, reconnect_required: true } } : current)
      }
    } finally {
      if (requestSequence === videoRequestSequence.current) setVideosLoading(false)
    }
  }

  useEffect(() => {
    videoController.current?.abort()
    if (!session?.connected) return

    const controller = new AbortController()
    videoController.current = controller
    void loadVideoPage(null, false, controller.signal)
    return () => controller.abort()
  }, [session?.connected, session?.channel?.channel_id])

  async function loadComments(videoId: string | null, pageToken: string | null, append: boolean) {
    commentController.current?.abort()
    const controller = new AbortController()
    commentController.current = controller
    const requestSequence = ++commentRequestSequence.current
    setCommentsLoading(true)
    setCommentsError(null)
    const query = pageToken ? `?page_token=${encodeURIComponent(pageToken)}` : ''

    
    try {
      const url = videoId 
        ? `/api/youtube/videos/${encodeURIComponent(videoId)}/comments${query}`
        : `/api/youtube/channel/comments${query}`;
      
      const page = await apiRequest<Page<CommentItem>>(
        url,
        { signal: controller.signal },
      )

      if (requestSequence !== commentRequestSequence.current || selectedVideoId.current !== videoId) return
      setComments((current) => (append ? [...current, ...page.items] : page.items))
      setNextCommentPage(page.next_page_token)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (requestSequence !== commentRequestSequence.current || selectedVideoId.current !== videoId) return
      setCommentsError(error instanceof Error ? error.message : 'Comments could not be loaded.')
    } finally {
      if (requestSequence === commentRequestSequence.current && selectedVideoId.current === videoId) {
        setCommentsLoading(false)
      }
    }
  }

  
  function selectVideo(video: VideoItem | null) {
    setSelectedComment(null)
    commentRequestSequence.current += 1
    commentController.current?.abort()
    selectedVideoId.current = video ? video.video_id : null
    setSelectedVideo(video)
    setComments([])
    setNextCommentPage(null)
    setCommentsError(null)
    void loadComments(video ? video.video_id : null, null, false)
  }


  const connectYouTube = async (permissionUpgrade = false) => { if (!session?.csrf_token) return; try { const res = await apiRequest<{authorization_url: string}>(permissionUpgrade ? "/auth/google/upgrade" : "/auth/google/start", { method: "POST", headers: { "X-CSRF-Token": session.csrf_token } }); window.location.assign(res.authorization_url); } catch (e) { setSessionError(e instanceof Error ? e.message : "OAuth setup is not ready."); } }; const disconnectYouTube = async () => { if (!session?.csrf_token || !window.confirm("Disconnect YouTube?")) return; try { await apiRequest("/auth/google/disconnect", { method: "POST", headers: { "X-CSRF-Token": session.csrf_token } }); window.location.reload(); } catch { setSessionError("Failed to disconnect."); } }; return { connectYouTube, disconnectYouTube,
    session, sessionLoading, sessionError,
    videos, videosLoading, videosError, nextVideoPage, loadVideoPage,
    selectedVideo, selectVideo,
    comments, commentsLoading, commentsError, nextCommentPage, loadComments,
    selectedComment, setSelectedComment
  }
}
