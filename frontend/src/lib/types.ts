export type ConnectionStatus = {
  csrf_token: string
  oauth_configured: boolean
  setup_message: string | null
  connected: boolean
  channel: {
    channel_id: string
    channel_title: string
    channel_thumbnail_url: string | null
    reconnect_required: boolean
    comments_scope_available: boolean
  } | null
}

export type VideoItem = {
  video_id: string
  title: string
  thumbnail_url: string | null
  published_at: string | null
  watch_url: string
}

export type CommentItem = {
  comment_id: string | null
  author_name: string
  author_thumbnail_url: string | null
  published_at: string | null
  text: string
  total_reply_count: number
  reply_status: string
  video_id: string | null
  video_title: string | null
}

export type Page<T> = { items: T[]; next_page_token: string | null }
export type ServiceStatus = 'checking' | 'available' | 'unavailable'

export type ReplyPreferences = {
  tone: 'Professional' | 'Friendly' | 'Casual'
  reply_length: 'Short' | 'Medium' | 'Detailed'
  language: 'English' | 'Hindi' | 'Hinglish'
  channel_instructions: string
  revision: number
}

export type PublishingMode = 'manual_review' | 'auto_on_agreement' | 'approve_all'

export type PublishingSettings = {
  mode: PublishingMode
  revision: number
}

export type PublicationStatus = 'sending' | 'posted' | 'failed' | 'unknown'

export type PublicationItem = {
  id: string
  channel_id: string
  video_id: string
  parent_comment_id: string
  reply_comment_id: string | null
  text: string
  status: PublicationStatus
  created_at: string | null
  resolved_at: string | null
  failure: Record<string, string> | null
  draft_revision: number
  // The comment snapshot stored on the draft, so history is readable without a second
  // YouTube request. Null only if the draft predates the snapshot.
  comment_text: string | null
}

export type PublicationPage = {
  items: PublicationItem[]
  total: number
}
