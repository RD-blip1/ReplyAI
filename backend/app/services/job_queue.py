"""Durable job claiming, lease recovery, retries, and shared budgets.

Everything here is expressed as conditional UPDATEs against the job tables, so the
database — not a worker's memory — decides who owns a job and when an interrupted one
becomes claimable again. No function trusts a read-then-write sequence for safety.

Retry policy is classified, not uniform:

- Provider budget denials (Groq/YouTube rate limits) are *environmental*: the attempt
  counter is refunded and the job waits, so a busy day cannot burn a job's retry budget.
- Temporary failures (timeouts, 5xx, connection errors) use exponential backoff with
  jitter up to the job's ``max_attempts``.
- Terminal conditions (deleted comment, revoked OAuth, policy skips) never retry.
- An unknown publication outcome never retries; it waits for reconciliation.
"""

import random
from datetime import timedelta

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChannelOwner,
    QueueWorker,
    RateLimitCounter,
    ReplyDraft,
    ReplyJob,
    ReplyPublication,
)
from app.models.job import ACTIVE_JOB_STATUSES, READY_JOB_STATUSES
from app.models.publication import OPEN_PUBLICATION_STATUSES
from app.models.session import utc_now

# Base delay doubles per attempt; jitter spreads concurrent retries.
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_MAX_SECONDS = 900
# Delay used when a shared budget denies the call; the budget window, not the job,
# decides when it may run.
BUDGET_RETRY_SECONDS = 60
# Recovery of an interrupted job is quick: the work is already partly done.
RECOVERY_RETRY_SECONDS = 5


def view(job: ReplyJob) -> dict:
    """Job state for the API. Safe codes only."""
    return {
        "id": job.id,
        "batch_id": job.batch_id,
        "channel_id": job.channel_id,
        "video_id": job.video_id,
        "comment_id": job.comment_id,
        "draft_id": job.draft_id,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "recoveries": job.recoveries,
        "last_error": job.last_error or {},
        "outcome": job.outcome or {},
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "lease_expires_at": (
            job.lease_expires_at.isoformat() if job.lease_expires_at else None
        ),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def backoff_delay(attempts: int) -> int:
    """Exponential backoff with full jitter, capped."""
    ceiling = min(RETRY_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)),
                  RETRY_BACKOFF_MAX_SECONDS)
    return random.randint(1, max(ceiling, 1))


async def enqueue(
    db: AsyncSession,
    owner: ChannelOwner,
    *,
    video_id: str,
    comment_ids: list[str],
    batch_id: str | None = None,
    max_attempts: int = 3,
    priority: int = 0,
) -> dict:
    """Queue one job per comment. Duplicates are refused by the active-job index.

    Returns created/duplicate counts. A comment that already has an active job keeps
    the existing job; a finished job does not block a new one.
    """
    created: list[str] = []
    duplicates = 0
    # Snapshot the owner's fields: a savepoint rollback below expires ORM attributes,
    # and background/async loading of them is not available on an AsyncSession.
    owner_id = owner.id
    channel_id = owner.channel_id
    for comment_id in dict.fromkeys(comment_ids):  # stable dedup within the request
        job = ReplyJob(
            channel_owner_id=owner_id,
            channel_id=channel_id,
            video_id=video_id,
            comment_id=comment_id,
            batch_id=batch_id,
            max_attempts=max_attempts,
            priority=priority,
        )
        # A savepoint confines an IntegrityError to this one insert, so a duplicate in
        # the middle of a request cannot discard the jobs already staged ahead of it.
        try:
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError:
            duplicates += 1
            continue
        created.append(job.id)
    if created:
        await db.commit()
    return {"created": created, "duplicates": duplicates}


async def claim_next(
    db: AsyncSession,
    worker_id: str,
    *,
    lease_seconds: int = 120,
    max_per_channel: int = 2,
    owner_id: str | None = None,
) -> ReplyJob | None:
    """Atomically take one due job, or None.

    The conditional UPDATE (``status='pending|retry'`` in the WHERE clause) is the
    authority: two workers can both see the same job, but only one UPDATE matches.
    The per-channel concurrency check reads live leases; it is advisory load-shedding,
    not a safety boundary.
    """
    now = utc_now()
    ready = (
        await db.execute(
            select(ReplyJob)
            .where(
                ReplyJob.status.in_(READY_JOB_STATUSES),
                ReplyJob.next_run_at <= now,
                *([ReplyJob.channel_owner_id == owner_id] if owner_id else []),
            )
            .order_by(ReplyJob.priority.desc(), ReplyJob.next_run_at, ReplyJob.created_at)
            .limit(10)
        )
    ).scalars().all()
    for candidate in ready:
        processing = await db.scalar(
            select(func.count())
            .select_from(ReplyJob)
            .where(
                ReplyJob.channel_id == candidate.channel_id,
                ReplyJob.status == "processing",
                ReplyJob.lease_expires_at > now,
            )
        )
        if (processing or 0) >= max_per_channel:
            continue
        result = await db.execute(
            update(ReplyJob)
            .where(
                ReplyJob.id == candidate.id,
                ReplyJob.status.in_(READY_JOB_STATUSES),
            )
            .values(
                status="processing",
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=ReplyJob.attempts + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await db.commit()
            job = await db.get(ReplyJob, candidate.id)
            # The claim was a conditional UPDATE with synchronize_session=False, so the
            # identity-mapped object still holds pre-claim values. Refresh so the caller
            # sees the status/attempts/lease it actually owns.
            await db.refresh(job)
            return job
    await db.rollback()
    return None


async def heartbeat(db: AsyncSession, job_id: str, worker_id: str, *, lease_seconds: int) -> bool:
    """Extend the lease only while this worker still owns the job.

    Returns False when ownership was lost (recovered by another worker, or cancelled),
    which tells the caller to stop before doing anything externally visible.
    """
    now = utc_now()
    result = await db.execute(
        update(ReplyJob)
        .where(ReplyJob.id == job_id, ReplyJob.worker_id == worker_id,
               ReplyJob.status.in_(("processing", "verified", "publish_claimed")))
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def finish(
    db: AsyncSession,
    job_id: str,
    worker_id: str,
    status: str,
    *,
    outcome: dict | None = None,
    error: dict | None = None,
) -> bool:
    """Move a claimed job to a terminal state. Only the current owner may finish it."""
    now = utc_now()
    result = await db.execute(
        update(ReplyJob)
        .where(
            ReplyJob.id == job_id,
            ReplyJob.worker_id == worker_id,
            ReplyJob.status.in_(("processing", "verified", "publish_claimed")),
        )
        .values(
            status=status,
            outcome=outcome or {},
            last_error=error or {},
            lease_expires_at=None,
            worker_id=None,
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def set_milestone(
    db: AsyncSession, job_id: str, worker_id: str, status: str, *, outcome: dict | None = None
) -> bool:
    """Persist an intermediate owned state (``verified`` / ``publish_claimed``).

    Written before the externally visible step it announces, so crash recovery can
    distinguish "models done, nothing sent" from "publication may be in flight".
    """
    now = utc_now()
    result = await db.execute(
        update(ReplyJob)
        .where(
            ReplyJob.id == job_id,
            ReplyJob.worker_id == worker_id,
            ReplyJob.status == "processing",
        )
        .values(status=status, outcome=outcome or {}, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def release_for_retry(
    db: AsyncSession,
    job_id: str,
    worker_id: str,
    *,
    code: str,
    delay_seconds: int,
    refund_attempt: bool = False,
    budget_denied: bool = False,
) -> bool:
    """Return a claimed job to the queue with a classified delay.

    ``refund_attempt`` is for environmental refusals (provider budget): the call was
    never made, so the job keeps its retry budget. ``budget_denied`` additionally flags
    the job so the UI can say *why* it is waiting.
    """
    now = utc_now()
    values: dict = {
        "status": "retry",
        "next_run_at": now + timedelta(seconds=max(delay_seconds, 1)),
        "lease_expires_at": None,
        "worker_id": None,
        "last_error": {"code": code},
        "updated_at": now,
    }
    if refund_attempt:
        # Portable max(attempts - 1, 0): ``greatest`` does not exist on SQLite.
        values["attempts"] = case(
            (ReplyJob.attempts > 0, ReplyJob.attempts - 1), else_=0
        )
    if budget_denied:
        values["outcome"] = {"waiting_for": "provider_budget"}
    result = await db.execute(
        update(ReplyJob)
        .where(
            ReplyJob.id == job_id,
            ReplyJob.worker_id == worker_id,
            ReplyJob.status.in_(("processing", "verified", "publish_claimed")),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def cancel(db: AsyncSession, owner_id: str, job_id: str) -> ReplyJob | None:
    """Cancel a job that has not started publishing. In-flight work is not interrupted
    mid-request; its publish step rechecks policy and durable state anyway."""
    job = await db.scalar(
        select(ReplyJob).where(ReplyJob.id == job_id, ReplyJob.channel_owner_id == owner_id)
    )
    if job is None or job.status not in ("pending", "retry"):
        return None
    now = utc_now()
    result = await db.execute(
        update(ReplyJob)
        .where(ReplyJob.id == job.id, ReplyJob.status.in_(("pending", "retry")))
        .values(
            status="cancelled",
            outcome={"reason": "cancelled_by_user"},
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        return None
    await db.refresh(job)
    return job


async def recover_interrupted(
    db: AsyncSession, *, lease_seconds: int = 120, max_recoveries: int = 3
) -> dict:
    """Reconcile jobs whose worker died, using durable state as the only evidence.

    Rules, in order — publication state always wins over job state:

    - The draft has an open publication attempt (``sending``/``unknown``/``posted``):
      the insert may have happened. ``posted`` → the job is ``published``; anything
      else → ``unknown``. Never retried.
    - Otherwise the job is returned to the queue (``pending``) with a short delay, or
      marked ``failed`` when its recovery cap is exhausted.
    """
    now = utc_now()
    stale = (
        await db.execute(
            select(ReplyJob).where(
                ReplyJob.status.in_(("processing", "verified", "publish_claimed")),
                or_(
                    ReplyJob.lease_expires_at.is_(None),
                    ReplyJob.lease_expires_at < now,
                ),
            )
        )
    ).scalars().all()
    recovered = 0
    marked_unknown = 0
    marked_published = 0
    failed = 0
    for job in stale:
        draft = await db.get(ReplyDraft, job.draft_id) if job.draft_id else None
        attempt = None
        if draft is not None:
            attempt = await db.scalar(
                select(ReplyPublication)
                .where(
                    ReplyPublication.draft_id == draft.id,
                    ReplyPublication.status.in_(OPEN_PUBLICATION_STATUSES),
                )
                .order_by(ReplyPublication.created_at.desc(), ReplyPublication.id.desc())
            )
        if attempt is not None:
            if attempt.status == "posted":
                await _finish_recovered(
                    db, job, "published",
                    outcome={"publication": {"status": "posted",
                                             "reply_comment_id": attempt.reply_comment_id}},
                )
                marked_published += 1
            else:
                await _finish_recovered(
                    db, job, "unknown",
                    outcome={"publication": {"status": "unknown"},
                             "reason": "publication_outcome_unknown"},
                    error={"code": "publish_outcome_unknown"},
                )
                marked_unknown += 1
            continue
        if job.recoveries >= max_recoveries:
            await _finish_recovered(
                db, job, "failed",
                error={"code": "worker_crash_loop"},
                outcome={"reason": "recovery_limit_exhausted"},
            )
            failed += 1
            continue
        result = await db.execute(
            update(ReplyJob)
            .where(
                ReplyJob.id == job.id,
                ReplyJob.status.in_(("processing", "verified", "publish_claimed")),
            )
            .values(
                status="pending",
                recoveries=ReplyJob.recoveries + 1,
                next_run_at=now + timedelta(seconds=RECOVERY_RETRY_SECONDS),
                lease_expires_at=None,
                worker_id=None,
                last_error={"code": "worker_interrupted"},
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        recovered += result.rowcount
        await db.commit()
    return {
        "recovered": recovered,
        "marked_unknown": marked_unknown,
        "marked_published": marked_published,
        "failed": failed,
    }


async def _finish_recovered(
    db: AsyncSession, job: ReplyJob, status: str, *, outcome: dict | None = None,
    error: dict | None = None,
) -> None:
    now = utc_now()
    await db.execute(
        update(ReplyJob)
        .where(
            ReplyJob.id == job.id,
            ReplyJob.status.in_(("processing", "verified", "publish_claimed")),
        )
        .values(
            status=status,
            outcome=outcome or {},
            last_error=error or {},
            lease_expires_at=None,
            worker_id=None,
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()


# --- Shared budgets -----------------------------------------------------------


async def try_acquire(
    db: AsyncSession, key: str, per_minute: int, per_day: int, *, commit: bool = True
) -> bool:
    """Atomically consume one token from a rolling minute/day budget.

    The conditional UPDATE never over-counts: two concurrent acquirers cannot both
    take the last token, because the loser's WHERE clause no longer matches after the
    winner's write. A missing row is inserted once; losing that insert race just means
    "not this tick".

    ``commit=False`` keeps the acquisition inside the caller's open transaction, so a
    budget token can be reserved atomically with a related claim and rolled back with
    it. In that mode a missing row is inserted without an immediate unique-race check;
    the caller's commit resolves it.
    """
    if per_minute <= 0 and per_day <= 0:
        return True
    now = utc_now()
    min_ago = now - timedelta(minutes=1)
    day_ago = now - timedelta(hours=24)
    result = await db.execute(
        update(RateLimitCounter)
        .where(
            RateLimitCounter.key == key,
            or_(RateLimitCounter.window_started_at <= min_ago,
                per_minute <= 0,
                RateLimitCounter.window_count < per_minute),
            or_(RateLimitCounter.day_started_at <= day_ago,
                per_day <= 0,
                RateLimitCounter.day_count < per_day),
        )
        .values(
            window_count=case(
                (RateLimitCounter.window_started_at <= min_ago, 1), else_=RateLimitCounter.window_count + 1
            ),
            window_started_at=case(
                (RateLimitCounter.window_started_at <= min_ago, now), else_=RateLimitCounter.window_started_at
            ),
            day_count=case(
                (RateLimitCounter.day_started_at <= day_ago, 1), else_=RateLimitCounter.day_count + 1
            ),
            day_started_at=case(
                (RateLimitCounter.day_started_at <= day_ago, now), else_=RateLimitCounter.day_started_at
            ),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        if commit:
            await db.commit()
        return True
    existing = await db.get(RateLimitCounter, key)
    if existing is None:
        db.add(RateLimitCounter(key=key, window_started_at=now, window_count=1,
                                day_started_at=now, day_count=1))
        if not commit:
            return True
        try:
            await db.commit()
            return True
        except IntegrityError:
            await db.rollback()
            return False
    # The window is exhausted. The conditional UPDATE matched nothing and left no
    # change, so there is nothing to roll back; avoiding a rollback here matters because
    # a rollback expires the caller's ORM objects, and an AsyncSession cannot lazily
    # reload them.
    return False


# --- Worker heartbeats --------------------------------------------------------


async def touch_worker(db: AsyncSession, worker_id: str) -> None:
    worker = await db.get(QueueWorker, worker_id)
    now = utc_now()
    if worker is None:
        db.add(QueueWorker(id=worker_id, heartbeat_at=now))
    else:
        worker.heartbeat_at = now
        worker.jobs_processed += 0
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()


async def count_job_processed(db: AsyncSession, worker_id: str) -> None:
    result = await db.execute(
        update(QueueWorker)
        .where(QueueWorker.id == worker_id)
        .values(jobs_processed=QueueWorker.jobs_processed + 1, heartbeat_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        await db.rollback()
        return
    await db.commit()


async def active_workers(db: AsyncSession, *, within_seconds: int = 60) -> list[dict]:
    cutoff = utc_now() - timedelta(seconds=within_seconds)
    rows = (
        (await db.execute(select(QueueWorker).where(QueueWorker.heartbeat_at >= cutoff)))
        .scalars()
        .all()
    )
    return [
        {"id": row.id, "heartbeat_at": row.heartbeat_at.isoformat(),
         "jobs_processed": row.jobs_processed}
        for row in rows
    ]


async def queue_stats(db: AsyncSession, owner_id: str) -> dict:
    rows = (
        await db.execute(
            select(ReplyJob.status, func.count())
            .where(ReplyJob.channel_owner_id == owner_id)
            .group_by(ReplyJob.status)
        )
    ).all()
    counts = {status: count for status, count in rows}
    due = await db.scalar(
        select(func.count())
        .select_from(ReplyJob)
        .where(
            ReplyJob.channel_owner_id == owner_id,
            ReplyJob.status.in_(READY_JOB_STATUSES),
            ReplyJob.next_run_at <= utc_now(),
        )
    )
    return {
        "counts": counts,
        "due_now": due or 0,
        "total": sum(counts.values()),
    }


def owner_for_channel(rows) -> ChannelOwner | None:
    return rows[0] if rows else None


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "BUDGET_RETRY_SECONDS",
    "READY_JOB_STATUSES",
    "RECOVERY_RETRY_SECONDS",
    "active_workers",
    "backoff_delay",
    "cancel",
    "claim_next",
    "count_job_processed",
    "enqueue",
    "finish",
    "heartbeat",
    "queue_stats",
    "recover_interrupted",
    "release_for_retry",
    "set_milestone",
    "touch_worker",
    "try_acquire",
    "view",
]
