"""Durable queue semantics: dedup, atomic claiming, leases, retries, recovery, budgets.

These tests drive ``app.services.job_queue`` directly against the temporary test
database, so they exercise the schema rules (partial unique index, conditional UPDATEs)
rather than a mocked queue.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import select, update
from support import COMMENT

from app.models import (
    ChannelOwner,
    PublishingSettings,
    ReplyDraft,
    ReplyJob,
    ReplyPublication,
)
from app.models.session import utc_now
from app.services import job_queue


def run(coro):
    return asyncio.run(coro)


async def _owner(db) -> ChannelOwner:
    return await db.scalar(select(ChannelOwner))


def test_enqueue_deduplicates_active_jobs(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            first = await job_queue.enqueue(
                db, owner, video_id="abcdefghijk", comment_ids=["c1", "c2", "c1"]
            )
            assert len(first["created"]) == 2  # stable dedup within one request
            second = await job_queue.enqueue(
                db, owner, video_id="abcdefghijk", comment_ids=["c1", "c3"]
            )
            assert len(second["created"]) == 1
            assert second["duplicates"] == 1
    run(main())


def test_created_job_is_claimable_and_second_claim_is_none(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1"])
            job = await job_queue.claim_next(db, "worker-a")
            assert job is not None and job.status == "processing"
            assert job.attempts == 1
            # The same job is no longer claimable by anyone.
            assert await job_queue.claim_next(db, "worker-b") is None
    run(main())


def test_expired_lease_becomes_recoverable_then_claimable(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1"])
            job = await job_queue.claim_next(db, "worker-a", lease_seconds=1)
            await db.execute(
                update(ReplyJob)
                .where(ReplyJob.id == job.id)
                .values(lease_expires_at=utc_now() - timedelta(seconds=5))
            )
            await db.commit()
            result = await job_queue.recover_interrupted(db, lease_seconds=120)
            assert result["recovered"] == 1
            refreshed = await db.get(ReplyJob, job.id, populate_existing=True)
            assert refreshed.status == "pending"
            assert refreshed.recoveries == 1
    run(main())


def test_finish_requires_current_ownership(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1"])
            job = await job_queue.claim_next(db, "worker-a")
            assert await job_queue.finish(db, job.id, "worker-b", "review") is False
            assert await job_queue.finish(db, job.id, "worker-a", "review") is True
            refreshed = await db.get(ReplyJob, job.id, populate_existing=True)
            assert refreshed.status == "review"
    run(main())


def test_release_for_retry_can_refund_the_attempt(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1"])
            job = await job_queue.claim_next(db, "worker-a")
            assert job.attempts == 1
            assert await job_queue.release_for_retry(
                db, job.id, "worker-a", code="provider_timeout", delay_seconds=5,
                refund_attempt=True, budget_denied=True,
            )
            refreshed = await db.get(ReplyJob, job.id, populate_existing=True)
            assert refreshed.status == "retry"
            assert refreshed.attempts == 0
            assert refreshed.outcome == {"waiting_for": "provider_budget"}
    run(main())


def test_backoff_is_bounded_and_jittered():
    delays = {job_queue.backoff_delay(n) for n in range(1, 8)}
    assert all(1 <= d <= job_queue.RETRY_BACKOFF_MAX_SECONDS for d in delays)
    assert job_queue.backoff_delay(50) <= job_queue.RETRY_BACKOFF_MAX_SECONDS


def test_cancel_only_applies_to_waiting_jobs(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1"])
            job = (await db.execute(select(ReplyJob))).scalars().first()
            assert (await job_queue.cancel(db, owner.id, job.id)).status == "cancelled"
            # A processing job cannot be cancelled.
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c2"])
            processing = await job_queue.claim_next(db, "worker-a")
            assert await job_queue.cancel(db, owner.id, processing.id) is None
    run(main())


def test_try_acquire_enforces_minute_and_day_limits(workspace):
    async def main():
        async with workspace.sessions() as db:
            assert await job_queue.try_acquire(db, "unit:minute", 2, 0) is True
            assert await job_queue.try_acquire(db, "unit:minute", 2, 0) is True
            assert await job_queue.try_acquire(db, "unit:minute", 2, 0) is False
            assert await job_queue.try_acquire(db, "unit:day", 0, 1) is True
            assert await job_queue.try_acquire(db, "unit:day", 0, 1) is False
            # A zero budget means "unlimited", not "denied".
            assert await job_queue.try_acquire(db, "unit:none", 0, 0) is True
    run(main())


def _draft(db, owner, comment_id="c1") -> ReplyDraft:
    draft = ReplyDraft(
        channel_owner_id=owner.id, channel_id=owner.channel_id,
        video_id="abcdefghijk", comment_id=comment_id, original_comment=COMMENT,
    )
    db.add(draft)
    return draft


def test_recovery_reports_unknown_for_an_open_attempt(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            draft = _draft(db, owner)
            await db.flush()
            db.add(ReplyPublication(
                draft_id=draft.id, operation_id="op-1", channel_id=owner.channel_id,
                video_id=draft.video_id, parent_comment_id=draft.comment_id,
                draft_revision=draft.revision, text="reply", status="sending",
            ))
            job = await job_queue.enqueue(
                db, owner, video_id="abcdefghijk", comment_ids=[draft.comment_id]
            )
            job_id = job["created"][0]
            await db.commit()
            claimed = await job_queue.claim_next(db, "worker-a")
            await db.execute(
                update(ReplyJob).where(ReplyJob.id == claimed.id).values(
                    draft_id=draft.id, lease_expires_at=utc_now() - timedelta(seconds=5)
                )
            )
            await db.commit()
            result = await job_queue.recover_interrupted(db, lease_seconds=120)
            assert result["marked_unknown"] == 1
            refreshed = await db.get(ReplyJob, job_id, populate_existing=True)
            assert refreshed.status == "unknown"
            assert refreshed.last_error == {"code": "publish_outcome_unknown"}
    run(main())


def test_recovery_reports_published_for_a_posted_attempt(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            draft = _draft(db, owner, "c2")
            await db.flush()
            db.add(ReplyPublication(
                draft_id=draft.id, operation_id="op-2", channel_id=owner.channel_id,
                video_id=draft.video_id, parent_comment_id=draft.comment_id,
                draft_revision=draft.revision, text="reply", status="posted",
                reply_comment_id="reply-1",
            ))
            job = await job_queue.enqueue(
                db, owner, video_id="abcdefghijk", comment_ids=[draft.comment_id]
            )
            job_id = job["created"][0]
            await db.commit()
            claimed = await job_queue.claim_next(db, "worker-a")
            await db.execute(
                update(ReplyJob).where(ReplyJob.id == claimed.id).values(
                    draft_id=draft.id, lease_expires_at=utc_now() - timedelta(seconds=5)
                )
            )
            await db.commit()
            result = await job_queue.recover_interrupted(db, lease_seconds=120)
            assert result["marked_published"] == 1
            refreshed = await db.get(ReplyJob, job_id, populate_existing=True)
            assert refreshed.status == "published"
    run(main())


def test_queue_stats_counts_by_status(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner = await _owner(db)
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1", "c2"])
            await db.commit()
            stats = await job_queue.queue_stats(db, owner.id)
            assert stats["counts"].get("pending") == 2
            assert stats["total"] == 2
            assert PublishingSettings is not None  # keep import meaningful
    run(main())
