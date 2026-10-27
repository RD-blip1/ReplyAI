"""Central automatic-decision policy: one pure function over every signal.

``decide(ctx)`` maps the facts about one comment and its draft to exactly one class:

    AUTO_PUBLISH · HUMAN_REVIEW · SKIP · POLICY_BLOCK · RETRY · TECHNICAL_FAILURE

Checks run in a fixed priority order (documented step by step in docs/architecture.md);
the first match wins. The existing content checks are not re-implemented here:
``publishing.automation_blocking_reason`` is called and its codes are mapped, so the
manual/automatic publication semantics stay defined in one place.

Agreement alone is never sufficient. Infrastructure failures can only produce RETRY or
TECHNICAL_FAILURE, never HUMAN_REVIEW or SKIP.
"""
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services import publishing, reply_policy
from app.services.stage_outcomes import outcome_kind

POLICY_VERSION = "automation-policy-v1"
CLASSES = ("AUTO_PUBLISH", "HUMAN_REVIEW", "SKIP", "POLICY_BLOCK", "RETRY", "TECHNICAL_FAILURE")
REQUIRED_STAGES = ("triage", "generation", "classification", "quality")
SKIP_TRIAGE = {"spam_or_suspicious": "spam", "harassment_or_abuse": "harassment",
               "off_topic": "off_topic", "unsupported_language": "unsupported_language"}
HUMAN_TRIAGE = {"requires_human_attention": "requires_human", "uncertain": "triage_uncertain",
                "generic_engagement": "context_required"}
REAUTH_CODES = {"reconnect_required", "insufficient_scope", "oauth_setup_required",
                "youtube_not_connected", "missing_auth", "needs_reauth"}
# Content codes from automation_blocking_reason → decision class.
BLOCKING_CLASS = {
    "triage_required": "HUMAN_REVIEW", "generation_required": "HUMAN_REVIEW",
    "review_required": "HUMAN_REVIEW", "confidence_insufficient": "HUMAN_REVIEW",
    "review_findings": "HUMAN_REVIEW", "models_disagree": "HUMAN_REVIEW",
    "manual_review_required": "HUMAN_REVIEW", "requires_human": "HUMAN_REVIEW",
    "context_required": "HUMAN_REVIEW", "spam": "SKIP", "harassment": "SKIP",
    "unsupported_language": "SKIP", "off_topic": "SKIP",
}
HIGH_RISK_INPUT = {"tag_characters", "bidi_control", "invisible_in_word", "confusable_url_host"}


@dataclass(frozen=True)
class Decision:
    decision_class: str
    decision_code: str
    reasons: list[str] = field(default_factory=list)
    execution: str = "none"  # "publish" | "none" | "retry"
    retry_delay_s: int | None = None
    policy_version: str = POLICY_VERSION
    dry_run: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DecisionContext:
    """Everything decide() reads. Pure data; building it is the caller's job."""
    draft: Any  # ReplyDraft or a stand-in with the same attributes
    mode: str
    paused: bool = False
    discovery_disabled: bool = False  # only for discovery-originated jobs
    stage_failures: dict = field(default_factory=dict)
    attempts_remaining: bool = True
    connection_code: str | None = None  # None = authorised with force-ssl
    revalidation: str | None = None  # unchanged|edited|gone|creator_replied|read_error
    own_comment: bool = False
    input_signals: dict | None = None
    input_signals_mode: str = "observe"
    allowlist: dict | None = None
    evidence: str = ""
    reply_policy_mode: str = "enforce_auto"
    caps_available: bool = True
    mode_changed: bool = False
    skip_threshold: float = 0.0
    grounding_variant: str = "v0"


def _decision(cls, code, *reasons, execution="none", retry=None, dry_run=False):
    return Decision(cls, code, [r for r in reasons if r], execution, retry, POLICY_VERSION, dry_run)


def two_llm_violation(draft) -> str | None:
    """Two-model invariant: primary generated, secondary classified and reviewed, models differ."""
    providers = getattr(draft, "providers", None) or {}
    primary, secondary = providers.get("primary") or {}, providers.get("secondary") or {}
    if not primary.get("model") or not secondary.get("model"):
        return "provider identities were not recorded"
    if (primary.get("base_url"), primary.get("model")) == (secondary.get("base_url"), secondary.get("model")):
        return "generator and verifier are the same model"
    if getattr(draft, "generation_status", None) != "complete" or not getattr(draft, "primary_classification", None):
        return "primary generation did not complete"
    if not getattr(draft, "secondary_classification", None):
        return "secondary classification did not run"
    if getattr(draft, "review_status", None) != "complete" or not getattr(draft, "quality", None):
        return "secondary review of the saved reply did not run"
    return None


def policy_violations(draft, ctx: "DecisionContext") -> list:
    return reply_policy.check(
        getattr(draft, "text", "") or "", comment=getattr(draft, "original_comment", "") or "",
        evidence=ctx.evidence, allowlist=ctx.allowlist,
        commenter_handle=getattr(draft, "source_author_display_name", None))


def _infrastructure(ctx) -> Decision | None:
    for stage in REQUIRED_STAGES:
        code = ctx.stage_failures.get(stage)
        if not code or code in ("skipped", "generation_required"):
            continue
        kind = outcome_kind(code)
        if kind == "budget_stopped":
            return _decision("RETRY", code, f"{stage} budget unavailable", execution="retry")
        if kind == "infra_transient" and ctx.attempts_remaining:
            return _decision("RETRY", code, f"{stage} provider failure is transient", execution="retry")
        if kind == "output_invalid":
            if ctx.attempts_remaining:
                return _decision("RETRY", code, f"{stage} output invalid; one retry", execution="retry")
            return _decision("TECHNICAL_FAILURE", "model_output_invalid", f"{stage} output invalid")
        return _decision("TECHNICAL_FAILURE", code, f"{stage} failed")
    return None


def decide(ctx: DecisionContext) -> Decision:
    draft = ctx.draft
    # Step 1: infrastructure is routed before any content judgement.
    infra = _infrastructure(ctx)
    if infra:
        return infra
    # Step 2: credentials.
    if ctx.connection_code in REAUTH_CODES:
        return _decision("TECHNICAL_FAILURE", "needs_reauth", ctx.connection_code)
    # Step 3: owner switches.
    if ctx.paused:
        return _decision("RETRY", "automation_paused", "automation is paused", execution="retry", retry=300)
    if ctx.discovery_disabled:
        return _decision("HUMAN_REVIEW", "automation_disabled", "automation was disabled")
    # Step 4: external state of the comment.
    if ctx.own_comment:
        return _decision("SKIP", "own_comment", "the channel wrote this comment")
    if ctx.revalidation == "gone":
        return _decision("SKIP", "comment_deleted", "the comment no longer exists")
    if ctx.revalidation == "creator_replied":
        return _decision("SKIP", "already_answered_by_creator", "the creator already replied")
    if ctx.revalidation == "edited":
        return _decision("HUMAN_REVIEW", "comment_edited", "the comment changed after drafting")
    if ctx.revalidation == "read_error":
        return _decision("RETRY", "revalidation_read_error", "could not re-read the comment",
                         execution="retry")

    triage = getattr(draft, "triage", None) or {}
    category = triage.get("category")
    # Step 5: triage. Skip categories, optionally gated by confidence (threshold 0 = always skip).
    if category in SKIP_TRIAGE:
        confidence = triage.get("confidence")
        confident = isinstance(confidence, (int, float)) and confidence >= ctx.skip_threshold
        if confident or ctx.skip_threshold <= 0:
            return _decision("SKIP", SKIP_TRIAGE[category], triage.get("reason"))
        return _decision("HUMAN_REVIEW", "triage_low_confidence_skip", triage.get("reason"))
    # Triage categories that need a person.
    if category in HUMAN_TRIAGE:
        return _decision("HUMAN_REVIEW", HUMAN_TRIAGE[category], triage.get("reason"))
    # Step 6: content signals. Language: the model's language claim disagrees with the script.
    language = triage.get("language") or {}
    signals = ctx.input_signals or {}
    profile = signals.get("script_profile")
    if language.get("script") and profile in ("Latn", "Deva", "mixed") and language["script"] != profile:
        return _decision("HUMAN_REVIEW", "language_uncertain",
                         f"model said {language['script']}, text is {profile}")
    # Answerability, only when evidence is supplied to the models.
    if ctx.grounding_variant != "v0" and triage.get("answer_source") == "creator_only":
        return _decision("HUMAN_REVIEW", "needs_creator_answer", triage.get("missing_information"))
    # Multi-intent: risky segments always go to a person.
    for segment in triage.get("segments") or []:
        if segment.get("risk") == "high" or segment.get("action") == "escalate":
            return _decision("HUMAN_REVIEW", "multi_intent_risk", segment.get("text_span"))
    # High-risk concealment, only when routing is switched on.
    if ctx.input_signals_mode == "route_high_risk" and HIGH_RISK_INPUT & set(signals.get("high_risk_kinds") or []):
        return _decision("POLICY_BLOCK", "input_obfuscation_suspected",
                         ", ".join(sorted(HIGH_RISK_INPUT & set(signals["high_risk_kinds"]))))
    # Sarcasm: either classifier flags possible sarcasm.
    for classification in (getattr(draft, "primary_classification", None),
                           getattr(draft, "secondary_classification", None)):
        if (classification or {}).get("sarcasm") in ("yes", "uncertain"):
            return _decision("HUMAN_REVIEW", "sarcasm_possible", "a classifier flagged sarcasm")

    # Step 7: verification, via the existing predicate; reply policy is evaluated
    # separately below so its code can be classed as POLICY_BLOCK.
    blocked = publishing.automation_blocking_reason(draft, ctx.mode, policy=False)
    if blocked:
        code, message = blocked
        if code == "quality_not_passed":
            quality = getattr(draft, "quality", None) or {}
            if str(quality.get("inappropriate_content") or "").casefold() != "pass":
                return _decision("POLICY_BLOCK", "quality_inappropriate", message)
            return _decision("HUMAN_REVIEW", "quality_not_passed", message)
        return _decision(BLOCKING_CLASS.get(code, "HUMAN_REVIEW"), code, message)

    # Step 8: deterministic reply policy.
    if ctx.reply_policy_mode == "enforce_auto":
        violation = reply_policy.blocking(policy_violations(draft, ctx))
        if violation:
            return _decision("POLICY_BLOCK", f"reply_policy:{violation.rule}", violation.detail)
    # Step 9: two-model invariant.
    broken = two_llm_violation(draft)
    if broken:
        return _decision("TECHNICAL_FAILURE", "two_llm_invariant_violated", broken)
    # Step 10: mode and caps. Manual mode is also covered by automation_blocking_reason.
    if ctx.mode not in publishing.AUTOMATIC_MODES:
        return _decision("HUMAN_REVIEW", "manual_review_required", "manual review mode")
    if not ctx.caps_available:
        return _decision("RETRY", "auto_publish_rate_limited", "hourly/daily cap reached",
                         execution="retry", retry=900)
    if ctx.mode_changed:
        return _decision("HUMAN_REVIEW", "automatic_mode_changed", "publishing mode changed")
    # Step 11: eligible.
    if ctx.mode == "dry_run":
        return _decision("AUTO_PUBLISH", "eligible", "dry run: nothing is sent", dry_run=True)
    return _decision("AUTO_PUBLISH", "eligible", "every automatic-publication check passed",
                     execution="publish")


# Worker job outcomes (status, code) → class, for codes decided before publication.
JOB_STATUS_CLASS = {"published": "AUTO_PUBLISH", "unknown": "AUTO_PUBLISH",
                    "skipped": "SKIP", "retry": "RETRY", "failed": "TECHNICAL_FAILURE",
                    "needs_reauth": "TECHNICAL_FAILURE"}
POLICY_BLOCK_CODES = {"quality_inappropriate", "input_obfuscation_suspected"}


def class_for_job(status: str, code: str | None) -> str:
    """The decision class implied by a finished job. Review splits by its code."""
    if status == "review":
        if code and (code.startswith("reply_policy:") or code in POLICY_BLOCK_CODES):
            return "POLICY_BLOCK"
        if code in ("publication_failed",):
            return "AUTO_PUBLISH"
        return "HUMAN_REVIEW"
    return JOB_STATUS_CLASS.get(status, "TECHNICAL_FAILURE")


def execution_result(status: str, code: str | None, dry_run: bool = False) -> str:
    if dry_run:
        return "dry_run_not_sent"
    return {"published": "posted", "unknown": "unknown", "retry": "deferred",
            "failed": "not_sent", "needs_reauth": "not_sent", "skipped": "not_sent"}.get(
        status, "failed" if code == "publication_failed" else "not_sent")
