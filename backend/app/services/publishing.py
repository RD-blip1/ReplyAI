"""Deciding whether a saved draft may be published, and what its attempt means.

The rule this module exists to enforce is that the server decides. The browser may say
what the user saw in the preview, but the text that would go public, the revision it
belongs to, the review, the approval, and the ownership are all read from the database.
A client that asks to publish something other than what is saved is refused rather than
obeyed.

Manual publication does not require classifier agreement; it is shown as information for
the person approving the reply. Auto-publish-on-agreement applies a separate, stricter
agreement rule in ``automation_blocking_reason``.
"""
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PublishedReplyGuard, ReplyDraft, ReplyPublication
from app.models.publication import OPEN_PUBLICATION_STATUSES
from app.models.session import utc_now
from app.services import reply_policy

PublishingMode = Literal["manual_review", "auto_on_agreement", "approve_all", "dry_run"]
AUTOMATIC_MODES: tuple[str, ...] = ("auto_on_agreement", "approve_all", "dry_run")
"""Modes in which a newly generated draft may be published without a person confirming it."""


def _utc(value: datetime | None) -> datetime | None:
    """Stamp a naive timestamp as UTC so one record reads the same from any session.

    A row just written holds aware datetimes and the same row re-read from the database
    can hold naive ones, which would make a replay answer differ from the original
    request for no reason the caller could see.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value

# The three checks the review model returns. "pass" means it found no issue; "concern"
# and "uncertain" are both reasons to stop, because neither is a clearance.
QUALITY_CHECKS = ("relevance", "unsupported_claims", "inappropriate_content")
AUTO_MIN_REVIEW_CONFIDENCE = 0.8
"""Minimum explicit secondary-review confidence for either automatic mode."""

# What the client is shown, and what decides whether a retry is offered.
BLOCKING_DRAFT = (
    (lambda draft: not (draft.text or "").strip(),
     "no_reply", "Save reply text before publishing."),
    (lambda draft: draft.review_status != "complete",
     "review_required", "Complete review of the saved revision before publishing."),
    (lambda draft: not draft.quality,
     "review_required", "Complete review of the saved revision before publishing."),
    (lambda draft: any(str((draft.quality or {}).get(check) or "").casefold() != "pass"
                       for check in QUALITY_CHECKS),
     "quality_not_passed", "The saved review did not pass every check. Edit and review it again."),
    (lambda draft: draft.human_decision != "approved",
     "approval_required", "Approve this draft locally before publishing it."),
)


def blocking_reason(draft: ReplyDraft) -> tuple[str, str] | None:
    """Return the code and message blocking publication, or None when it may proceed."""
    for blocked, code, message in BLOCKING_DRAFT:
        if blocked(draft):
            return code, message
    return None


def automation_blocking_reason(
    draft: ReplyDraft, mode: str, *, policy: bool = True, allowlist: dict | None = None,
    evidence: str = "",
) -> tuple[str, str] | None:
    """Additional requirements for publishing a newly generated draft automatically.

    ``policy`` adds the deterministic reply-policy check after the agreement check;
    ``automation_policy.decide`` passes False and applies it itself so it can class the
    result as POLICY_BLOCK. Existing codes and their order are unchanged.
    """
    if not draft.triage:
        return "triage_required", "Contextual triage is required."
    triage_cat = draft.triage.get("category")
    if triage_cat == "spam_or_suspicious":
        return "spam", "Comment triage classified it as spam."
    if triage_cat == "harassment_or_abuse":
        return "harassment", "Comment triage classified it as harassment or abuse."
    if triage_cat == "unsupported_language":
        return "unsupported_language", "Comment triage classified it as unsupported language."
    if triage_cat == "requires_human_attention":
        return "requires_human", "Comment triage classified it as requiring human attention."
    if triage_cat == "generic_engagement":
        return "context_required", "Generic engagement needs human review for video relevance."
    if triage_cat != "on_topic":
        return "off_topic", f"Comment triage classified it as {triage_cat}."

    if draft.generation_status != "complete":
        return "generation_required", "A complete primary-model generation is required."

    if not draft.primary_classification or not draft.secondary_classification:
        return "review_required", "Both model classifications are required."
    classifications = (draft.primary_classification, draft.secondary_classification)
    if any(
        value in ("uncertain", "other")
        for classification in classifications
        for value in (classification.get(key) for key in ("sentiment", "intent"))
    ):
        return "confidence_insufficient", "A model classification is uncertain."
    if draft.review_status != "complete" or not draft.quality:
        return "review_required", "A complete secondary-model review is required."
    quality = draft.quality
    if any(str(quality.get(check) or "").casefold() != "pass" for check in QUALITY_CHECKS):
        return "quality_not_passed", "The secondary-model review did not pass every check."
    confidence = quality.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or confidence < AUTO_MIN_REVIEW_CONFIDENCE):
        return "confidence_insufficient", "The secondary-model review confidence is too low."
    if quality.get("findings"):
        return "review_findings", "The secondary-model review reported findings for human review."
    if draft.agreement != "agree":
        return "models_disagree", "The models did not clearly agree; keep this reply for review."
    if policy and settings.reply_policy_mode == "enforce_auto":
        violation = reply_policy.blocking(reply_policy.check(
            getattr(draft, "text", "") or "", comment=getattr(draft, "original_comment", "") or "",
            evidence=evidence,
            allowlist=allowlist, commenter_handle=getattr(draft, "source_author_display_name", None)))
        if violation:
            return (f"reply_policy:{violation.rule}",
                    "The reply contains content that needs a person to approve it.")
    if mode not in AUTOMATIC_MODES:
        return "manual_review_required", "This publishing mode does not allow automatic replies."
    return None


def review_summary(draft: ReplyDraft) -> dict:
    """The review facts a person should see next to a confirm button."""
    quality = draft.quality or {}
    return {
        "review_status": draft.review_status,
        "checks": {check: quality.get(check) for check in QUALITY_CHECKS},
        "confidence": quality.get("confidence"),
        "findings": quality.get("findings") or [],
        "agreement": draft.agreement,
        # Deterministic reply-policy findings; warnings only for manual publication.
        "policy": [violation.as_dict() for violation in reply_policy.check(
            draft.text or "", comment=draft.original_comment or "",
            commenter_handle=getattr(draft, "source_author_display_name", None))],
    }


def view(row: ReplyPublication | None) -> dict | None:
    """Present one attempt, normalizing an abandoned in-flight row to unknown.

    A stored ``sending`` means the request was committed but never resolved. The only
    way a reader outside the publishing request can observe that is when the writer
    stopped, so it is reported as ``unknown``: the honest answer, and the one that stops
    anything from being retried on a guess.
    """
    if row is None:
        return None
    status = "unknown" if row.status == "sending" else row.status
    return {
        "id": row.id,
        "operation_id": row.operation_id,
        "status": status,
        "channel_id": row.channel_id,
        "video_id": row.video_id,
        "parent_comment_id": row.parent_comment_id,
        "draft_revision": row.draft_revision,
        "text": row.text,
        "reply_comment_id": row.reply_comment_id,
        "failure": row.failure or {},
        "sent_at": _utc(row.sent_at),
        "created_at": _utc(row.created_at),
        "resolved_at": _utc(row.resolved_at),
        # Only a documented rejection posted nothing, so it is the only state a person
        # may deliberately repeat. An unknown outcome is never offered a retry.
        "retryable": status == "failed",
    }


async def open_attempt(db: AsyncSession, draft_id: str) -> ReplyPublication | None:
    """The draft's in-flight, unresolved, or already public attempt, if there is one.

    The partial unique index makes this at most one row, so this lookup cannot miss a
    second active attempt, and the index is what actually refuses a concurrent insert.
    """
    return await db.scalar(
        select(ReplyPublication)
        .where(
            ReplyPublication.draft_id == draft_id,
            ReplyPublication.status.in_(OPEN_PUBLICATION_STATUSES),
        )
        .order_by(ReplyPublication.created_at.desc(), ReplyPublication.id.desc())
    )


async def latest_attempt(db: AsyncSession, draft_id: str) -> ReplyPublication | None:
    """The most recent attempt of any status, for reading a draft's posting history."""
    return await db.scalar(
        select(ReplyPublication)
        .where(ReplyPublication.draft_id == draft_id)
        .order_by(ReplyPublication.created_at.desc(), ReplyPublication.id.desc())
        .limit(1)
    )


async def unresolved_for_owner(db: AsyncSession, owner_id: str) -> ReplyPublication | None:
    """Any attempt under this owner whose outcome is still open.

    Deleting retained work must not be able to erase the record of a request that may
    already be public, so this blocks that path while an attempt is unresolved.
    """
    return await db.scalar(
        select(ReplyPublication)
        .join(ReplyDraft, ReplyDraft.id == ReplyPublication.draft_id)
        .where(
            ReplyDraft.channel_owner_id == owner_id,
            ReplyPublication.status.in_(("sending", "unknown")),
        )
        .limit(1)
    )


async def guard_for(
    db: AsyncSession, channel_id: str, parent_comment_id: str
) -> PublishedReplyGuard | None:
    """The retained record of a ReplyAI reply against this parent comment, if any.

    The unique index makes this at most one row. It is a convenience for reporting a
    precise reason; the reservation insert is what actually refuses a second reply.
    """
    return await db.scalar(
        select(PublishedReplyGuard).where(
            PublishedReplyGuard.channel_id == channel_id,
            PublishedReplyGuard.parent_comment_id == parent_comment_id,
        )
    )


def guard_status(guard: PublishedReplyGuard) -> str:
    """A guard's status with an abandoned reservation reported as unknown.

    ``sending`` means a request was reserved and its writer stopped, so the honest and
    safe answer is that the reply may exist.
    """
    return "unknown" if guard.status == "sending" else guard.status


def guard_message(guard: PublishedReplyGuard) -> tuple[str, str]:
    """Why this comment cannot be replied to again, in the caller's vocabulary."""
    if guard_status(guard) == "posted":
        return (
            "already_replied",
            (
                "This comment already has a reply published to YouTube by ReplyAI. "
                "ReplyAI keeps that record even if the draft is deleted, so it will "
                "not post a second reply here."
            ),
        )
    if guard.reply_comment_id:
        return (
            "already_replied",
            (
                "ReplyAI already published a reply to this comment: "
                f"{guard.reply_comment_id}. It will not post another one here."
            ),
        )
    return (
        "publication_unresolved",
        (
            "An earlier attempt to reply to this comment has an unresolved outcome. "
            "Check the comment's replies on YouTube before trying again, so the reply "
            "is not posted twice."
        ),
    )


async def release_guard(
    db: AsyncSession, guard_id: str, *, reply_comment_id: str | None = None,
    status: str = "posted",
) -> None:
    """Resolve a reservation the caller already owns, or release it if nothing was posted.

    ``status`` is ``posted`` for a confirmed reply and ``unknown`` for an ambiguous one.
    A definitively refused attempt created nothing, so its guard is deleted instead and
    a deliberate retry is free to reserve the comment again.
    """
    if status == "failed":
        await db.execute(
            delete(PublishedReplyGuard).where(PublishedReplyGuard.id == guard_id))
        return
    await db.execute(
        update(PublishedReplyGuard)
        .where(PublishedReplyGuard.id == guard_id)
        .values(status=status, reply_comment_id=reply_comment_id, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
