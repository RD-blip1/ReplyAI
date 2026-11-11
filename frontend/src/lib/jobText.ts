import { explain } from './decisions'
import type { ReplyJob } from './types'

export function errorCode(job: ReplyJob): string | null {
  const code = job.last_error?.code;
  return typeof code === 'string' ? code : null;
}

// Why a job is waiting; the specific reason is in last_error.code (B1).
const WAIT_REASONS: Record<string, string> = {
  auto_publish_rate_limited: 'automatic-publication cap reached',
  youtube_budget_exhausted: 'YouTube read budget used up',
  connection_changed: 'channel connection changed',
  local_budget_exhausted: 'LLM request budget used up',
  rate_limited: 'model provider rate limit',
};

export function decision(job: ReplyJob): string | null {
  const value = job.outcome?.decision;
  const waiting = job.outcome?.waiting_for;
  if (typeof waiting === 'string' && job.status === 'retry') {
    const code = errorCode(job);
    const reason = (code && WAIT_REASONS[code]) || code || waiting;
    // A verified job resumes at publication without new model calls.
    return value === 'models_complete' ? `Waiting: ${reason} · reply verified, will resume at publishing`
      : `Waiting: ${reason}`;
  }
  if (typeof value === 'string') return value;
  return typeof waiting === 'string' ? `waiting: ${waiting}` : null;
}

// Human-readable job notes. The raw code stays available as a secondary, technical line.
const JOB_NOTES: Record<string, string> = {
  published: 'Reply published',
  would_publish: 'Dry run: reply would be published (nothing sent)',
  dry_run_not_sent: 'Dry run: reply would be published (nothing sent)',
  models_disagree: 'The two models disagreed',
  skipped_spam: 'Skipped: spam detected',
  skipped_harassment: 'Skipped: harassment or abuse',
  skipped_off_topic: 'Skipped: not about this video',
  skipped_unsupported_language: 'Skipped: language not supported',
  draft_exists: 'A draft already existed; left for you',
  manual_review_required: 'Publishing mode is manual review',
  unknown_outcome: 'Publication outcome unknown; check YouTube',
  publish_outcome_unknown: 'Publication outcome unknown; check YouTube',
  publication_failed: 'YouTube rejected the reply',
  generation_failed: 'The reply could not be generated',
  automatic_mode_changed: 'Automatic publishing was turned off before sending',
  comment_deleted: 'The comment was deleted',
  creator_already_replied: 'You already replied on YouTube',
  models_complete: 'Reply generated and checked',
  not_eligible: 'Not eligible for automatic publication',
  video_not_in_channel_uploads: 'The video is not in this channel',
}

export type JobNote = { text: string; code: string | null }

export function jobNote(job: ReplyJob): JobNote | null {
  const waiting = decision(job)
  if (waiting?.startsWith('Waiting:')) return { text: waiting, code: errorCode(job) }
  const outcome = job.outcome ?? {}
  // The job's own result (published, skipped_spam, ...) wins over the policy code behind it.
  const final = typeof outcome.decision === 'string' && outcome.decision in JOB_NOTES ? outcome.decision : null
  const raw = [final, outcome.decision_code, outcome.decision, outcome.execution_result, errorCode(job)]
    .find((value): value is string => typeof value === 'string' && value.length > 0) ?? null
  if (!raw) return null
  const dry = outcome.execution_result === 'dry_run_not_sent' || outcome.decision === 'would_publish'
  const text = dry ? JOB_NOTES.would_publish : (JOB_NOTES[raw] ?? explain(raw))
  const codes = [...new Set([outcome.decision_code, outcome.decision, errorCode(job)]
    .filter((value): value is string => typeof value === 'string' && value.length > 0))]
  return { text, code: codes.join(' · ') || null }
}
