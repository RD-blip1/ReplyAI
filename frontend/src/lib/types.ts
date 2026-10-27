export type ConnectionStatus = {
  simulation?: boolean
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

export type PublishingMode = 'manual_review' | 'auto_on_agreement' | 'approve_all' | 'dry_run'

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

export type JobStatus =
  | 'pending'
  | 'retry'
  | 'processing'
  | 'verified'
  | 'publish_claimed'
  | 'published'
  | 'review'
  | 'skipped'
  | 'failed'
  | 'unknown'
  | 'needs_reauth'
  | 'cancelled'

export type ReplyJob = {
  id: string
  batch_id: string | null
  channel_id: string
  video_id: string
  comment_id: string
  draft_id: string | null
  status: JobStatus
  attempts: number
  max_attempts: number
  recoveries: number
  last_error: Record<string, unknown>
  outcome: Record<string, unknown>
  next_run_at: string | null
  lease_expires_at: string | null
  created_at: string | null
  finished_at: string | null
}

export type JobList = { items: ReplyJob[]; total: number }

export type JobStats = {
  counts: Record<string, number>
  due_now: number
  total: number
}

export type EnqueueResult = {
  created: string[]
  duplicates: number
  batch_id: string
}

export type DecisionClass =
  | 'AUTO_PUBLISH'
  | 'HUMAN_REVIEW'
  | 'SKIP'
  | 'POLICY_BLOCK'
  | 'RETRY'
  | 'TECHNICAL_FAILURE'

export type AutomationSettings = {
  enabled: boolean
  paused: boolean
  lookback_hours: number
  revision: number
  enabled_at: string | null
  discovery_cursor: string | null
  last_poll_started_at: string | null
  last_poll_finished_at: string | null
  next_poll_at: string | null
  last_error: Record<string, unknown> | null
}

export type AutomationSummary = {
  window: string
  decisions: { decision_class: DecisionClass; execution_result: string | null; count: number }[]
  discovery: Record<string, number>
}

export type PolicyViolation = { rule: string; level: string; span: number[] | null; detail: string }

export type AutomationDecision = {
  id: string
  draft_id: string | null
  job_id: string | null
  comment_id: string
  video_id: string
  decision_class: DecisionClass
  decision_code: string
  execution_result: string | null
  trace: Record<string, unknown>
  policy_version: string
  flags: Record<string, unknown>
  created_at: string
  resolved_at: string | null
  resolution: string | null
}

export type ReviewItem = {
  decision_id: string
  draft_id: string
  job_id: string | null
  video_id: string
  comment_id: string
  comment: string
  reply: string
  revision: number
  decision_class: DecisionClass
  decision_code: string
  reasons: string[]
  primary_classification: Record<string, string> | null
  secondary_classification: Record<string, string> | null
  agreement: string
  quality: Record<string, unknown> | null
  policy: PolicyViolation[]
  input_signals: { high_risk?: boolean; high_risk_kinds?: string[] } | null
  triage: Record<string, unknown>
  created_at: string
}
