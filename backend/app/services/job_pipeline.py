"""Processing one claimed batch job: snapshot → triage → generate → verify → decide.

The pipeline reuses the existing draft machinery (revision-guarded persistence, the
guarded publication path, the usage ledger) rather than reimplementing it. What it adds
is the durable job contract around it:

- Triage runs for every job regardless of workflow engine, because automation policy
  requires a contextual relevance decision and the legacy engine never ran triage.
- Publication always goes through ``publish_generated_draft`` →
  ``publish_saved_draft`` → ``claim``/``settle``, the same guarded path the synchronous
  UI uses, including its atomic mode recheck, so disabling automatic publishing during a
  batch is honored immediately before the insert.
- Failures are classified: environmental (provider budget, transient network) wait or
  retry with backoff; terminal (deleted comment, revoked grant, policy skip) never
  retry; an unknown publication outcome never retries and waits for reconciliation.
- A crash after verification is resumed at publication, not regenerated, using the
  draft revision recorded in the job's own ``outcome``.

The job is read into plain values at the start (``_Job``) because a rollback anywhere in
the pipeline expires ORM instances, and an ``AsyncSession`` cannot lazily reload them.
"""

import asyncio
import logging
import uuid
from typing import Any, NamedTuple

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ChannelOwner,
    PublishingSettings,
    ReplyDraft,
    ReplyJob,
    ReplyPreferences,
    YouTubeConnection,
)
from app.models.session import utc_now
from app.routers.drafts import identifiers
from app.routers.publication import publish_generated_draft
from app.services import (
    draft_workflow,
    job_queue,
    llm,
    publishing,
    reply_preferences,
    video_context,
)
from app.services.youtube_api import (
    YouTubeFailure,
    credentials_for_connection,
    get_owned_comment,
    video_belongs_to_channel,
)

logger = logging.getLogger(__name__)

# Triage categories that never produce a reply, per channel policy.
SKIP_CATEGORIES = {
    "spam_or_suspicious": "skipped_spam",
    "harassment_or_abuse": "skipped_harassment",
    "unsupported_language": "skipped_unsupported_language",
    "off_topic": "skipped_off_topic",
}
# Triage categories that stop generation but stay visible to a human.
HUMAN_CATEGORIES = {
    "requires_human_attention": "requires_human",
    "uncertain": "triage_uncertain",
    "generic_engagement": "context_required",
}
# Decision codes meaning "policy says skip", from either the pipeline's own routing or
# the publishing service's blocking reasons.
SKIP_DECISIONS = {
    "skipped_spam", "skipped_harassment", "skipped_unsupported_language", "skipped_off_topic",
    "spam", "harassment", "unsupported_language", "off_topic",
}
# Decision codes meaning the credential must be restored by a person.
REAUTH_DECISIONS = {
    "reconnect_required", "insufficient_scope", "oauth_setup_required",
    "youtube_not_connected", "missing_auth",
}
# Decision codes meaning "wait and try again" rather than "someone look at this".
WAIT_DECISIONS = {"auto_publish_rate_limited", "connection_changed"}


class JobAborted(Exception):
    """The job stopped safely; ``status`` and ``code`` describe the outcome.

    ``retry`` is handled centrally by ``process_job`` (one release), so helpers only
    raise it and never release the job themselves.
    """

    def __init__(
        self,
        status: str,
        code: str,
        *,
        outcome: dict | None = None,
        delay_seconds: int | None = None,
        refund_attempt: bool = False,
        budget_denied: bool = False,
    ) -> None:
        self.status = status
        self.code = code
        self.outcome = outcome or {}
        self.delay_seconds = delay_seconds
        self.refund_attempt = refund_attempt
        self.budget_denied = budget_denied
        super().__init__(code)


class _Job(NamedTuple):
    """A claimed job's immutable-for-this-run fields."""

    id: str
    owner_id: str
    channel_id: str
    video_id: str
    comment_id: str
    attempts: int
    max_attempts: int
    verified_revision: int | None

    @classmethod
    def of(cls, job: ReplyJob) -> "_Job":
        outcome = job.outcome or {}
        verified = outcome.get("draft_revision")
        return cls(
            id=job.id,
            owner_id=job.channel_owner_id,
            channel_id=job.channel_id,
            video_id=job.video_id,
            comment_id=job.comment_id,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            verified_revision=verified if outcome.get("decision") == "models_complete"
            and isinstance(verified, int) else None,
        )


async def _budget(
    db: AsyncSession, key: str, per_minute: int, per_day: int, job: _Job, label: str
) -> None:
    """Consume a shared provider token or ask the caller to release the job for retry."""
    if await job_queue.try_acquire(db, key, per_minute, per_day):
        return
    raise JobAborted(
        "retry", f"{label}_budget_exhausted",
        delay_seconds=job_queue.BUDGET_RETRY_SECONDS, refund_attempt=True, budget_denied=True,
    )


async def _heartbeat(db: AsyncSession, job: _Job, worker_id: str) -> None:
    if not await job_queue.heartbeat(
        db, job.id, worker_id, lease_seconds=settings.job_lease_seconds
    ):
        raise JobAborted("lost", "lease_lost")


async def _connection_for(db: AsyncSession, channel_id: str) -> YouTubeConnection | None:
    """Newest usable credential for the job's channel.

    Connections are session-scoped; the worker is not a session, so it resolves by
    channel id and refuses connections flagged for reconnection. A channel whose only
    connections need reconnecting is a human problem, reported as ``needs_reauth``.
    """
    rows = (
        (
            await db.execute(
                select(YouTubeConnection)
                .where(YouTubeConnection.channel_id == channel_id)
                .order_by(YouTubeConnection.connected_at.desc(), YouTubeConnection.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return next((row for row in rows if not row.reconnect_required), None)


async def _ensure_draft(
    db: AsyncSession, job: _Job, connection: YouTubeConnection
) -> ReplyDraft:
    """Load or authoritatively create the draft. Text is always re-read from YouTube."""
    existing = await db.scalar(
        select(ReplyDraft).where(
            ReplyDraft.channel_owner_id == job.owner_id,
            ReplyDraft.comment_id == job.comment_id,
        )
    )
    if existing is not None:
        await db.execute(
            update(ReplyJob).where(ReplyJob.id == job.id).values(draft_id=existing.id)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return existing

    await _budget(db, "youtube:reads", settings.youtube_reads_per_minute, 0, job, "youtube_reads")
    try:
        credentials = await credentials_for_connection(connection, db)
        if not await video_belongs_to_channel(connection, credentials, job.video_id):
            raise JobAborted("skipped", "video_unavailable",
                             outcome={"reason": "video_not_in_channel_uploads"})
        text = await get_owned_comment(connection, credentials, job.video_id, job.comment_id)
    except YouTubeFailure as exc:
        if exc.code in ("reconnect_required", "insufficient_scope"):
            raise JobAborted("needs_reauth", exc.code) from None
        if exc.code in ("youtube_temporarily_unavailable", "google_temporarily_unavailable"):
            raise JobAborted("retry", exc.code,
                             delay_seconds=job_queue.backoff_delay(job.attempts)) from None
        raise JobAborted("skipped", exc.code, outcome={"reason": exc.message}) from None

    draft = ReplyDraft(
        channel_owner_id=job.owner_id,
        channel_id=job.channel_id,
        video_id=job.video_id,
        comment_id=job.comment_id,
        original_comment=text,
        providers=identifiers(),
        operation_token=str(uuid.uuid4()),
    )
    db.add(draft)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent path created the draft first. Reuse it rather than overwrite.
        await db.rollback()
        existing = await db.scalar(
            select(ReplyDraft).where(
                ReplyDraft.channel_owner_id == job.owner_id,
                ReplyDraft.comment_id == job.comment_id,
            )
        )
        if existing is None:
            raise JobAborted("retry", "draft_create_conflict",
                             delay_seconds=job_queue.RECOVERY_RETRY_SECONDS) from None
        draft = existing
    await db.execute(
        update(ReplyJob).where(ReplyJob.id == job.id).values(draft_id=draft.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return draft


async def _video_context(
    db: AsyncSession, job: _Job, connection: YouTubeConnection
) -> tuple[str, str]:
    await _budget(db, "youtube:reads", settings.youtube_reads_per_minute, 0, job, "youtube_reads")
    try:
        credentials = await credentials_for_connection(connection, db)
        context: dict[str, Any] = await video_context.get_video_context(credentials, job.video_id)
    except Exception:  # noqa: BLE001 - context is supporting evidence, never required
        return "", ""
    return (context.get("title") or "", context.get("description") or "")


async def run_generation(
    db: AsyncSession,
    draft: ReplyDraft,
    *,
    video_title: str,
    video_description: str,
    channel_title: str,
    preferences: dict[str, str],
) -> ReplyDraft:
    """Triage, then generate/classify/review, then persist once, revision-guarded.

    The LangGraph flow in linear form; it works under either configured workflow engine
    because the batch worker *is* the engine here.
    """
    revision, token = draft.revision, draft.operation_token
    if token is None:
        raise ValueError("Batch generation requires a claimed operation token.")
    original = {"comment": draft.original_comment}
    failures: dict[str, str] = {}
    usage: dict = dict(draft.token_usage or {})
    spent: list[dict] = []
    triage_value = None
    text = ""
    primary = None
    secondary = None
    quality = None

    async def remember(stage: str, result) -> None:
        spent.extend(
            draft_workflow.usage_attempts(
                result, draft_id=draft.id, revision=revision, operation_token=token,
                operation="regenerate", stage=stage,
            )
        )

    triage = await draft_workflow.call_stage(
        "secondary",
        llm.Triage,
        "Assess if the viewer's comment is relevant to the video discussion. on_topic means clearly "
        "relevant; generic_engagement means generic praise like 'nice video'; off_topic means unrelated "
        "to the video context; spam_or_suspicious means spam/scam; harassment_or_abuse means abusive "
        "content; unsupported_language means not in the expected language; requires_human_attention "
        "means too complex or sensitive; uncertain means insufficient evidence to decide. Give a short "
        "reason and a confidence score.",
        {
            "comment": draft.original_comment,
            "video_title": video_title or "Unknown",
            "video_description": video_description or "None",
            "channel_title": channel_title,
        },
    )
    await remember("triage", triage)
    triage_value = triage.value
    if triage.error:
        failures["triage"] = triage.error

    category = (triage_value or {}).get("category")
    if category in ("on_topic", "generic_engagement"):
        generated, classified = await asyncio.gather(
            draft_workflow.call_stage(
                "primary", llm.Generation, reply_preferences.generation_instruction(),
                {
                    "comment": draft.original_comment,
                    "video_id": draft.video_id,
                    "channel": {"channel_id": draft.channel_id, "title": channel_title},
                    "response_preferences": preferences,
                },
            ),
            draft_workflow.call_stage(
                "secondary", llm.Classification,
                "Independently classify the original comment sentiment and intent.", original,
            ),
        )
        await remember("generation", generated)
        await remember("classification", classified)
        primary = generated.value["classification"] if generated.value else None
        text = generated.value["reply"] if generated.value else ""
        secondary = classified.value
        usage["generation"] = generated.usage
        usage["classification"] = classified.usage
        for stage, stage_error in (("generation", generated.error),
                                   ("classification", classified.error)):
            if stage_error:
                failures[stage] = stage_error
                usage.pop(stage, None)
        if text:
            reviewed = await draft_workflow.call_stage(
                "secondary", llm.Quality,
                "Assess the proposed reply for relevance, unsupported claims, and inappropriate "
                "content. pass means no issue identified; concern means an issue; uncertain means "
                "insufficient evidence. Give concise findings and a confidence score from 0 to 1 "
                "for this review. This is not human approval.",
                {**original, "reply": text},
            )
            await remember("quality", reviewed)
            quality = reviewed.value
            usage["quality"] = reviewed.usage
            if reviewed.error:
                failures["quality"] = reviewed.error
                usage.pop("quality", None)
        else:
            failures["quality"] = "generation_required"
    else:
        failures["generation"] = "skipped"

    return await draft_workflow.persist_result(
        db, draft, revision=revision, operation_token=token, text=text,
        primary=primary, secondary=secondary, triage=triage_value, quality=quality,
        failures=failures, usage=usage, spent=spent,
    )


def _triage_route(category: str | None) -> tuple[str, str] | None:
    """Terminal routing decided by triage alone, before any generation spend."""
    if category in SKIP_CATEGORIES:
        return "skipped", SKIP_CATEGORIES[category]
    if category in HUMAN_CATEGORIES:
        return "review", HUMAN_CATEGORIES[category]
    return None


async def _automation_step(
    db: AsyncSession, job: _Job, draft: ReplyDraft, connection: YouTubeConnection
) -> None:
    """Apply the channel's current publishing policy to the just-generated revision."""
    setting = await db.get(PublishingSettings, job.owner_id)
    mode = setting.mode if setting else "manual_review"
    revision = setting.revision if setting else 0
    if mode == "manual_review":
        raise JobAborted(
            "review", "manual_review_required",
            outcome={"decision": "manual_review_required",
                     "reason": "The channel's publishing mode is manual review."},
        )

    decision = await publish_generated_draft(
        draft, None, db, expected_mode=mode, expected_revision=revision, connection=connection,
    )
    decision = decision or {"decision": "not_eligible"}
    code = decision.get("decision")

    if code == "published":
        published = decision.get("published") or {}
        raise JobAborted(
            "published", "published",
            outcome={"decision": "published", "reply_comment_id": published.get("reply_comment_id")},
        )
    if code == "attempt_recorded":
        attempt_status = decision.get("attempt_status")
        if attempt_status == "posted":
            raise JobAborted("published", "published", outcome={"decision": "published"})
        if attempt_status in ("unknown", "sending"):
            raise JobAborted("unknown", "publish_outcome_unknown",
                             outcome={"decision": "unknown_outcome"})
        raise JobAborted("review", "publication_failed",
                         outcome={"decision": "publication_failed"})
    if code in SKIP_DECISIONS:
        raise JobAborted("skipped", code,
                         outcome={"decision": code, "reason": decision.get("reason")})
    if code in REAUTH_DECISIONS:
        raise JobAborted("needs_reauth", code, outcome={"decision": code})
    if code in WAIT_DECISIONS:
        raise JobAborted(
            "retry", code, delay_seconds=900, refund_attempt=True, budget_denied=True,
            outcome={"decision": code},
        )
    if code == "automatic_mode_changed":
        raise JobAborted(
            "review", "automatic_mode_changed",
            outcome={"decision": code,
                     "reason": "Automatic publishing was turned off before this reply was sent."},
        )
    if code == "publish_outcome_unknown":
        raise JobAborted("unknown", "publish_outcome_unknown",
                         outcome={"decision": "unknown_outcome"})
    # Every remaining decision (blocked quality, disagreement, conflicts, dry-run
    # outcomes, policy changes) is something a person should see.
    raise JobAborted("review", code or "not_eligible",
                     outcome={"decision": code, "reason": decision.get("reason")})


async def process_job(db: AsyncSession, job: ReplyJob, worker_id: str) -> str:
    """Run one claimed job to a terminal state or a classified retry.

    Returns the final status for this pass: published, review, skipped, failed, unknown,
    needs_reauth, retry, or lost.
    """
    snapshot = _Job.of(job)
    try:
        owner = await db.get(ChannelOwner, snapshot.owner_id)
        if owner is None:
            await job_queue.finish(db, snapshot.id, worker_id, "failed",
                                   error={"code": "owner_deleted"})
            return "failed"

        connection = await _connection_for(db, snapshot.channel_id)
        if connection is None:
            await job_queue.finish(db, snapshot.id, worker_id, "needs_reauth",
                                   error={"code": "reconnect_required"},
                                   outcome={"reason": "No usable YouTube connection."})
            return "needs_reauth"

        draft = await _ensure_draft(db, snapshot, connection)

        # Durable publication state always wins over job state.
        attempt = await publishing.open_attempt(db, draft.id)
        if attempt is not None:
            view = publishing.view(attempt) or {}
            if view.get("status") == "posted":
                await job_queue.finish(
                    db, snapshot.id, worker_id, "published",
                    outcome={"decision": "published",
                             "reply_comment_id": view.get("reply_comment_id")},
                )
                return "published"
            await job_queue.finish(db, snapshot.id, worker_id, "unknown",
                                   error={"code": "publish_outcome_unknown"},
                                   outcome={"decision": "unknown_outcome"})
            return "unknown"

        resumable = (
            snapshot.verified_revision is not None
            and draft.revision == snapshot.verified_revision
            and draft.human_decision == "pending"
            and draft.publication_token is None
        )
        if (draft.text or "").strip() and not resumable:
            # Existing saved work is never overwritten by a batch job.
            await job_queue.finish(
                db, snapshot.id, worker_id, "review",
                outcome={"decision": "draft_exists",
                         "reason": "A saved draft already exists for this comment."},
            )
            return "review"

        if not resumable:
            # Take a fresh generation claim; a crashed earlier generation's token is
            # discarded (its persist becomes stale, which is exactly what we want).
            generation_token = str(uuid.uuid4())
            claimed = await db.execute(
                update(ReplyDraft)
                .where(ReplyDraft.id == draft.id, ReplyDraft.publication_token.is_(None))
                .values(operation_token=generation_token, updated_at=utc_now())
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                await db.rollback()
                await job_queue.finish(db, snapshot.id, worker_id, "review",
                                       outcome={"decision": "draft_busy"})
                return "review"
            await db.commit()
            await db.refresh(draft)

            await _budget(db, "llm:secondary", settings.llm_requests_per_minute,
                          settings.llm_requests_per_day, snapshot, "llm_secondary")
            await _budget(db, "llm:primary", settings.llm_requests_per_minute,
                          settings.llm_requests_per_day, snapshot, "llm_primary")
            video_title, video_description = await _video_context(db, snapshot, connection)
            await _heartbeat(db, snapshot, worker_id)

            saved_preferences = await db.get(ReplyPreferences, snapshot.owner_id)
            try:
                draft = await run_generation(
                    db, draft,
                    video_title=video_title, video_description=video_description,
                    channel_title=connection.channel_title,
                    preferences=reply_preferences.context(saved_preferences),
                )
            except draft_workflow.StaleWorkflowResult:
                await job_queue.finish(db, snapshot.id, worker_id, "review",
                                       outcome={"decision": "draft_conflict"},
                                       error={"code": "stale_review"})
                return "review"

            await _heartbeat(db, snapshot, worker_id)

            category = (draft.triage or {}).get("category")
            route = _triage_route(category)
            if route is not None:
                status, code = route
                await job_queue.finish(
                    db, snapshot.id, worker_id, status,
                    outcome={"decision": code, "triage_category": category,
                             "reason": (draft.triage or {}).get("reason")},
                )
                return status

            stage_failures = draft.failure or {}
            generation_error = stage_failures.get("generation")
            if generation_error and generation_error != "skipped":
                if generation_error == "rate_limited":
                    raise JobAborted(
                        "retry", generation_error,
                        delay_seconds=job_queue.BUDGET_RETRY_SECONDS,
                        refund_attempt=True, budget_denied=True,
                    )
                if generation_error in ("provider_timeout", "provider_unavailable",
                                        "transient_error", "provider_request_failed"):
                    if snapshot.attempts >= snapshot.max_attempts:
                        await job_queue.finish(db, snapshot.id, worker_id, "failed",
                                               error={"code": generation_error})
                        return "failed"
                    raise JobAborted(
                        "retry", generation_error,
                        delay_seconds=job_queue.backoff_delay(snapshot.attempts),
                    )
                await job_queue.finish(db, snapshot.id, worker_id, "review",
                                       error={"code": generation_error},
                                       outcome={"decision": "generation_failed"})
                return "review"

            if not (draft.text or "").strip():
                # Triage allowed generation but no reply text survived.
                await job_queue.finish(db, snapshot.id, worker_id, "review",
                                       error={"code": "generation_empty"},
                                       outcome={"decision": "generation_failed"})
                return "review"
        else:
            await _heartbeat(db, snapshot, worker_id)

        await job_queue.set_milestone(
            db, snapshot.id, worker_id, "verified",
            outcome={"decision": "models_complete", "draft_revision": draft.revision},
        )
        await _automation_step(db, snapshot, draft, connection)
        return "unreachable"  # _automation_step always raises JobAborted

    except JobAborted as aborted:
        if aborted.status == "lost":
            logger.warning("event=job_lease_lost job=%s", snapshot.id)
            return "lost"
        if aborted.status == "retry":
            released = await job_queue.release_for_retry(
                db, snapshot.id, worker_id, code=aborted.code,
                delay_seconds=aborted.delay_seconds
                or job_queue.backoff_delay(snapshot.attempts),
                refund_attempt=aborted.refund_attempt, budget_denied=aborted.budget_denied,
            )
            return "retry" if released else "lost"
        finished = await job_queue.finish(
            db, snapshot.id, worker_id, aborted.status, error={"code": aborted.code},
            outcome=aborted.outcome,
        )
        return aborted.status if finished else "lost"
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code", "worker_http_error")
        if code == "publish_outcome_unknown":
            await job_queue.finish(db, snapshot.id, worker_id, "unknown",
                                   error={"code": code},
                                   outcome={"decision": "unknown_outcome"})
            return "unknown"
        logger.warning("event=job_http_failure job=%s code=%s", snapshot.id, code)
        await job_queue.finish(db, snapshot.id, worker_id, "review", error={"code": code},
                               outcome={"decision": "worker_http_error"})
        return "review"
    except Exception as exc:
        logger.exception("event=job_unexpected_failure job=%s type=%s", snapshot.id,
                         type(exc).__name__)
        if snapshot.attempts >= snapshot.max_attempts:
            await job_queue.finish(db, snapshot.id, worker_id, "failed",
                                   error={"code": "unexpected_error"})
            return "failed"
        await job_queue.release_for_retry(
            db, snapshot.id, worker_id, code="unexpected_error",
            delay_seconds=job_queue.backoff_delay(snapshot.attempts),
        )
        return "retry"
