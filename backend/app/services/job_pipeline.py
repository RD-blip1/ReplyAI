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
    llm_budget,
    publishing,
    reply_preferences,
    revalidation,
    video_context,
)
from app.services import (
    usage as usage_service,
)
from app.services.stage_outcomes import outcome_kind
from app.services.youtube_api import (
    YouTubeFailure,
    credentials_for_connection,
    get_owned_comment_snapshot,
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


async def connection_for(db: AsyncSession, channel_id: str) -> YouTubeConnection | None:
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
        source = await get_owned_comment_snapshot(connection, credentials, job.video_id, job.comment_id)
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
        original_comment=source.text,
        source_updated_at=source.updated_at,
        source_text_sha256=revalidation.text_hash(source.text),
        source_author_display_name=source.author_display_name,
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
    db: AsyncSession, draft: ReplyDraft, *, video_title: str, video_description: str,
    channel_title: str, preferences: dict[str, str], job_id: str | None = None,
    checkpoint=None, publishing_mode: str | None = None,
) -> ReplyDraft:
    """Run the shared sequence with attempt budgets and revision-fenced persistence."""
    from app.services import stage_pipeline
    revision, token = draft.revision, draft.operation_token
    if token is None:
        raise ValueError("Batch generation requires a claimed operation token.")
    spent = []
    usage = dict(draft.token_usage or {})

    async def remember(stage, result, elapsed, schema, instruction):
        spent.extend(draft_workflow.usage_attempts(
            result, draft_id=draft.id, revision=revision, operation_token=token,
            operation="regenerate", stage=stage))
        if result.usage and not result.error:
            usage[stage] = result.usage
        else:
            usage.pop(stage, None)

    async def progress():
        if checkpoint is None:
            return
        try:
            await checkpoint()
        except BaseException:
            for row in spent:
                row["applied"] = False
            await usage_service.record_attempts(spent)
            raise

    ctx = {"original_comment": draft.original_comment, "video_id": draft.video_id,
           "channel_id": draft.channel_id, "channel_title": channel_title,
           "video_title": video_title, "video_description": video_description,
           "response_preferences": preferences, "publishing_mode": publishing_mode}
    run = await stage_pipeline.run_stages(
        draft, ctx, operation="regenerate", checkpoint=progress, on_result=remember,
        providers=llm_budget.provider if settings.llm_budget_mode != "off" else llm.provider)
    failures = run.failures
    if job_id and any(outcome_kind(failures.get(stage)) != "ok"
                      for stage in ("triage", "generation", "classification", "quality")
                      if failures.get(stage) not in ("skipped", "generation_required")):
        previous = (draft.failure or {}).get("_worker_retry", {})
        count = (previous.get("failures", 0) if previous.get("job_id") == job_id
                 and previous.get("revision") == revision else 0)
        charge = any(outcome_kind(failures.get(stage)) in ("infra_transient", "infra_permanent", "output_invalid")
                     for stage in ("triage", "generation", "classification", "quality")
                     if failures.get(stage) not in ("skipped", "generation_required"))
        failures["_worker_retry"] = {"job_id": job_id, "revision": revision, "failures": count + int(charge)}
    return await draft_workflow.persist_result(
        db, draft, revision=revision, operation_token=token, text=run.text,
        primary=run.primary, secondary=run.secondary, triage=run.triage, quality=run.quality,
        failures=failures, usage=usage, spent=spent)


def _triage_route(category: str | None, confidence=None) -> tuple[str, str] | None:
    """Terminal routing decided by triage alone, before any generation spend.

    Selective skip: with ``triage_skip_threshold`` > 0, a skip category below the
    threshold goes to a person instead (the default 0 always skips).
    """
    if category in SKIP_CATEGORIES:
        threshold = settings.triage_skip_threshold
        if threshold > 0 and not (isinstance(confidence, (int, float)) and confidence >= threshold):
            return "review", "triage_low_confidence_skip"
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
    meta = {key: decision[key] for key in ("decision_class", "decision_code", "execution_result")
            if decision.get(key)}
    try:
        _route_publication(job, decision)
    except JobAborted as aborted:
        aborted.outcome = {**meta, **aborted.outcome}
        raise


def _route_publication(job: _Job, decision: dict) -> None:
    """Map the publication decision to a job status (always raises JobAborted)."""
    code = decision.get("decision")

    if code in {"comment_deleted", "creator_already_replied"}:
        raise JobAborted("skipped", code)
    if code in {"source_read_error", "youtube_budget_exhausted", "thread_scan_incomplete"}:
        if code != "youtube_budget_exhausted" and job.attempts >= job.max_attempts:
            raise JobAborted("failed", code)
        raise JobAborted("retry", code, delay_seconds=job_queue.backoff_delay(job.attempts),
                         refund_attempt=code == "youtube_budget_exhausted",
                         budget_denied=code == "youtube_budget_exhausted")
    if code == "automation_paused":
        raise JobAborted("retry", code, delay_seconds=300, refund_attempt=True,
                         outcome={"decision": code})

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
    """Process one job and record its central decision class."""
    from app.services import automation_decisions
    job_id = job.id
    status = await _process_job(db, job, worker_id)
    await automation_decisions.record_job(job_id, status)
    return status


async def _process_job(db: AsyncSession, job: ReplyJob, worker_id: str) -> str:
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

        connection = await connection_for(db, snapshot.channel_id)
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
        retry_marker = (draft.failure or {}).get("_worker_retry", {})
        retry_owned = (retry_marker.get("job_id") == snapshot.id and retry_marker.get("revision") == draft.revision
                       and draft.human_decision == "pending" and draft.operation_token is None)
        if (draft.text or "").strip() and not resumable and not retry_owned:
            # Existing saved work is never overwritten by a batch job.
            await job_queue.finish(
                db, snapshot.id, worker_id, "review",
                outcome={"decision": "draft_exists",
                         "reason": "A saved draft already exists for this comment."},
            )
            return "review"

        if settings.revalidate_before_publish:
            credentials = await credentials_for_connection(connection, db)
            source_state = await revalidation.revalidate(connection, credentials, draft, db, budgeted=True)
            if source_state.kind == "creator_replied":
                raise JobAborted("skipped", "already_answered_by_creator")
            if source_state.kind == "gone":
                raise JobAborted("skipped", "comment_deleted")
            if source_state.kind == "edited":
                raise JobAborted("review", "comment_edited")
            if source_state.kind == "read_error":
                if source_state.reason in {"reconnect_required", "insufficient_scope"}:
                    raise JobAborted("needs_reauth", source_state.reason)
                denied = source_state.reason == "youtube_budget_exhausted"
                if not denied and snapshot.attempts >= snapshot.max_attempts:
                    raise JobAborted("failed", source_state.reason)
                raise JobAborted("retry", source_state.reason,
                                 delay_seconds=job_queue.backoff_delay(snapshot.attempts),
                                 refund_attempt=denied, budget_denied=denied)

        if not resumable:
            # Take a fresh generation claim; a crashed earlier generation's token is
            # discarded (its persist becomes stale, which is exactly what we want).
            generation_token = str(uuid.uuid4())
            claimed = await db.execute(
                update(ReplyDraft)
                .where(ReplyDraft.id == draft.id, ReplyDraft.publication_token.is_(None),
                       ReplyDraft.revision == draft.revision,
                       ReplyDraft.operation_token == draft.operation_token,
                       ReplyDraft.human_decision == "pending")
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

            video_title, video_description = await _video_context(db, snapshot, connection)
            await _heartbeat(db, snapshot, worker_id)

            saved_preferences = await db.get(ReplyPreferences, snapshot.owner_id)
            try:
                draft = await run_generation(
                    db, draft,
                    video_title=video_title, video_description=video_description,
                    channel_title=connection.channel_title,
                    preferences=reply_preferences.context(saved_preferences),
                    job_id=snapshot.id,
                    checkpoint=lambda: _heartbeat(db, snapshot, worker_id),
                    publishing_mode=getattr(await db.get(PublishingSettings, snapshot.owner_id), "mode", None),
                )
            except draft_workflow.StaleWorkflowResult:
                await job_queue.finish(db, snapshot.id, worker_id, "review",
                                       outcome={"decision": "draft_conflict"},
                                       error={"code": "stale_review"})
                return "review"

            await _heartbeat(db, snapshot, worker_id)

            # Infrastructure is routed before any content-level triage decision.
            stage_failures = draft.failure or {}
            failure_count = max(snapshot.attempts, stage_failures.get("_worker_retry", {}).get("failures", 0))
            for stage in ("triage", "generation", "classification", "quality"):
                code = stage_failures.get(stage)
                if not code or code in ("skipped", "generation_required"):
                    continue
                kind = outcome_kind(code)
                if kind == "budget_stopped":
                    raise JobAborted("retry", code, delay_seconds=job_queue.BUDGET_RETRY_SECONDS,
                                     refund_attempt=True, budget_denied=True)
                if kind == "infra_transient" and failure_count < snapshot.max_attempts:
                    delay = (max(60, min(900, stage_failures.get("retry_after_seconds", 60)))
                             if code == "rate_limited" else job_queue.backoff_delay(snapshot.attempts))
                    raise JobAborted("retry", code, delay_seconds=delay,
                                     refund_attempt=code == "rate_limited")
                if kind == "output_invalid" and failure_count < 2:
                    raise JobAborted("retry", code,
                                     delay_seconds=job_queue.backoff_delay(snapshot.attempts))
                raise JobAborted("failed", "model_output_invalid" if kind == "output_invalid" else code,
                                 outcome={"decision_class": "TECHNICAL_FAILURE", "stage": stage})

            category = (draft.triage or {}).get("category")
            route = _triage_route(category, (draft.triage or {}).get("confidence"))
            if route is not None:
                status, code = route
                await job_queue.finish(
                    db, snapshot.id, worker_id, status,
                    outcome={"decision": code, "triage_category": category,
                             "reason": (draft.triage or {}).get("reason")},
                )
                return status

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
