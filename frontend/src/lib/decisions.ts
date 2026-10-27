import type { DecisionClass } from './types'

export const CLASS_LABELS: Record<DecisionClass, string> = {
  AUTO_PUBLISH: 'Auto-published',
  HUMAN_REVIEW: 'Needs review',
  SKIP: 'Skipped',
  POLICY_BLOCK: 'Blocked by policy',
  RETRY: 'Will retry',
  TECHNICAL_FAILURE: 'Technical failure',
}

export const CLASS_STYLES: Record<DecisionClass, string> = {
  AUTO_PUBLISH: 'bg-success/20 text-success border-success/30',
  HUMAN_REVIEW: 'bg-brand/20 text-brand-light border-brand/30',
  SKIP: 'bg-dark-600 text-text-muted border-dark-500',
  POLICY_BLOCK: 'bg-danger/20 text-danger border-danger/30',
  RETRY: 'bg-warning/20 text-warning border-warning/30',
  TECHNICAL_FAILURE: 'bg-danger/20 text-danger border-danger/30',
}

// Plain-language reasons for the codes a creator is most likely to see.
const CODE_TEXT: Record<string, string> = {
  eligible: 'Every automatic-publication check passed.',
  models_disagree: 'The two models read the comment differently.',
  confidence_insufficient: 'A model was not confident enough.',
  quality_not_passed: 'The reviewer model did not pass every check on the reply.',
  quality_inappropriate: 'The reviewer model flagged the reply as inappropriate.',
  review_findings: 'The reviewer model left findings for a person.',
  sarcasm_possible: 'The comment may be sarcastic.',
  comment_edited: 'The comment changed after the reply was drafted.',
  already_answered_by_creator: 'You already replied to this comment on YouTube.',
  comment_deleted: 'The comment no longer exists.',
  requires_human: 'The comment needs your personal attention.',
  context_required: 'Generic engagement; check it fits the video.',
  triage_uncertain: 'The relevance check was unsure.',
  needs_creator_answer: 'Only you can answer this question.',
  input_obfuscation_suspected: 'The comment contains hidden characters.',
  two_llm_invariant_violated: 'Both roles used the same model, so the reply was not independently checked.',
  automation_paused: 'Automation is paused.',
  manual_review_required: 'Publishing mode is manual review.',
  rate_limited: 'The model provider asked us to slow down.',
  skipped_spam: 'Spam or promotion.',
  skipped_harassment: 'Harassment or abuse.',
  skipped_off_topic: 'Not about this video.',
  skipped_unsupported_language: 'Language not supported.',
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
}

export function explain(code: string): string {
  if (code.startsWith('reply_policy:')) {
    const rule = code.split(':')[1]
    return `The reply ${RULE_TEXT[rule] ?? `broke policy rule ${rule}`}.`
  }
  return CODE_TEXT[code] ?? code.replaceAll('_', ' ')
}
