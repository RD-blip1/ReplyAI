import type { DecisionClass } from './types'

export const CLASS_LABELS: Record<DecisionClass, string> = {
  AUTO_PUBLISH: 'Auto-published',
  HUMAN_REVIEW: 'Needs review',
  SKIP: 'Skipped',
  POLICY_BLOCK: 'Blocked by policy',
  RETRY: 'Will retry',
  TECHNICAL_FAILURE: 'Technical failure',
}

// AUTO_PUBLISH means "eligible for automatic publication". In dry run nothing is sent, so
// the badge must not say "Auto-published" (the job itself then waits for a person).
export const DRY_RUN_RESULT = 'dry_run_not_sent'
export function isDryRun(executionResult?: unknown, decision?: unknown): boolean {
  return executionResult === DRY_RUN_RESULT || decision === 'would_publish'
}
export function decisionBadge(cls: DecisionClass, executionResult?: unknown, decision?: unknown): { label: string; style: string } {
  if (cls === 'AUTO_PUBLISH' && isDryRun(executionResult, decision)) {
    return { label: 'Dry run: would publish', style: 'badge badge-green badge-dashed' }
  }
  return { label: CLASS_LABELS[cls], style: CLASS_STYLES[cls] }
}

export const CLASS_STYLES: Record<DecisionClass, string> = {
  AUTO_PUBLISH: 'badge badge-green',
  HUMAN_REVIEW: 'badge badge-amber',
  SKIP: 'badge',
  POLICY_BLOCK: 'badge badge-red',
  RETRY: 'badge badge-amber',
  TECHNICAL_FAILURE: 'badge badge-red',
}

// Plain-language reasons for the codes a creator is most likely to see.
const CODE_TEXT: Record<string, string> = {
  eligible: 'Every automatic-publication check passed',
  models_disagree: 'The two models read the comment differently',
  confidence_insufficient: 'A model was not confident enough',
  quality_not_passed: 'The reviewer model did not pass every check on the reply',
  quality_inappropriate: 'The reviewer model flagged the reply as inappropriate',
  review_findings: 'The reviewer model left findings for a person',
  sarcasm_possible: 'The comment may be sarcastic',
  comment_edited: 'The comment changed after the reply was drafted',
  already_answered_by_creator: 'You already replied to this comment on YouTube',
  comment_deleted: 'The comment no longer exists',
  requires_human: 'The comment needs your personal attention',
  context_required: 'Generic engagement; check it fits the video',
  triage_uncertain: 'The relevance check was unsure',
  needs_creator_answer: 'Only you can answer this question',
  input_obfuscation_suspected: 'The comment contains hidden characters',
  injection_suspected: 'The comment tries to give instructions to the AI',
  sensitive_topic: 'Sensitive situation (account, money, legal or safety); please answer personally',
  multi_intent_risk: 'Part of the comment needs your judgement',
  language_uncertain: 'The language label did not match the text',
  triage_low_confidence_skip: 'The relevance check was unsure whether to skip it',
  two_llm_invariant_violated: 'Both roles used the same model, so the reply was not independently checked',
  automation_paused: 'Automation is paused',
  manual_review_required: 'Publishing mode is manual review',
  rate_limited: 'The model provider asked us to slow down',
  skipped_spam: 'Spam or promotion',
  skipped_harassment: 'Harassment or abuse',
  skipped_off_topic: 'Not about this video',
  skipped_unsupported_language: 'Language not supported',
}

const RULE_TEXT: Record<string, string> = {
  R1: 'repeats private contact details from the comment',
  R2: 'contains contact details that are not on your allow-list',
  R4: 'contains a link that is not on your allow-list',
  R5: 'mentions another account',
  R6: 'makes a promise or commitment',
  R7: 'mentions money, discounts or promo codes',
  R8: 'claims official approval',
  R9: 'contains hidden characters',
  R11: 'contains an evaluation canary',
  R12: 'repeats text the commenter told the AI to write',
}

export function explain(code: string): string {
  if (code.startsWith('reply_policy:')) {
    const rule = code.split(':')[1]
    return `The reply ${RULE_TEXT[rule] ?? `broke policy rule ${rule}`}`
  }
  return CODE_TEXT[code] ?? code.replaceAll('_', ' ')
}

// Inbox/Videos reply statuses (derived on the server from publication state, the latest
// current automation decision and the draft). One place for their colours and grouping.
export const STATUS_STYLES: Record<string, string> = {
  Posted: 'badge badge-green',
  Approved: 'badge badge-green',
  'Draft ready': 'badge',
  'Needs review': 'badge badge-amber',
  'Blocked by policy': 'badge badge-red',
  Skipped: 'badge',
  Waiting: 'badge badge-amber',
  Unconfirmed: 'badge badge-amber',
  Failed: 'badge badge-red',
  'Technical failure': 'badge badge-red',
  Rejected: 'badge',
}
const DEFAULT_STATUS_STYLE = 'badge'

export const statusStyle = (status: string | null | undefined) =>
  (status && STATUS_STYLES[status]) || DEFAULT_STATUS_STYLE

// Statuses that wait for the creator; shown under the "Needs review" tab.
export const ATTENTION_STATUSES = ['Needs review', 'Blocked by policy', 'Unconfirmed']
export const DRAFT_STATUSES = ['Draft ready', 'Approved']
