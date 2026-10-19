"""Phase 4B: budget and scheduling on real time windows.

Every window is a real clock comparison against ``utc_now()``: the tests either walk a
window back to where a real clock would already have moved on (hour/day/minute rollovers
are far too slow to wait for) or use a genuinely short real window (lease expiry, retry
delay) and wait for it.

Covered:
- hourly auto-publish window: final slot allowed, next rejected, rollover resets count,
- daily auto-publish token: exhausted day rejects, 24h rollover resets,
- an hourly rejection must not spend a daily token,
- minute provider window rollover,
- a budget refusal refunds the job's attempt and schedules ``next_run_at``,
- ``claim_next`` honours ``next_run_at``, and backoff stays inside its bounds,
- a real short lease expires in real time and becomes recoverable.
"""

import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.config import settings
from app.models import (
    ChannelOwner,
    PublishingSettings,
    RateLimitCounter,
    ReplyJob,
)
from app.models.session import utc_now
from app.routers.publication import reserve_automatic_slot
from app.services import job_queue


def run(coro):
    return asyncio.run(coro)


async def _owner_id(db) -> str:
    owner = await db.scalar(select(ChannelOwner))
    return owner.id


async def _revision(db) -> int:
    setting = await db.scalar(select(PublishingSettings))
    return setting.revision


def set_mode(workspace, mode) -> int:
    current = workspace.client.get("/api/publishing-settings").json()
    response = workspace.client.put(
        "/api/publishing-settings", headers=workspace.headers,
        json={"mode": mode, "revision": current["revision"],
              "confirm_automation": mode != "manual_review"},
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


async def _seed_counter(db, key, *, window_count=None, day_count=None,
                        window_started_at=None, day_started_at=None) -> None:
    row = await db.scalar(select(RateLimitCounter).where(RateLimitCounter.key == key))
    if row is None:
        row = RateLimitCounter(key=key, window_started_at=utc_now(), window_count=0,
                               day_started_at=utc_now(), day_count=0)
        db.add(row)
    if window_count is not None:
        row.window_count = window_count
    if day_count is not None:
        row.day_count = day_count
    if window_started_at is not None:
        row.window_started_at = window_started_at
    if day_started_at is not None:
        row.day_started_at = day_started_at
    await db.commit()


async def _hourly_count(db, owner_id) -> int:
    setting = await db.get(PublishingSettings, owner_id, populate_existing=True)
    return setting.auto_publish_count


def test_hourly_window_final_slot_then_rejection_then_rollover(workspace):
    """A full window is refused and an elapsed one starts counting again."""
    set_mode(workspace, "approve_all")

    async def main():
        async with workspace.sessions() as db:
            owner_id, revision = await _owner_id(db), await _revision(db)
            setting = await db.scalar(select(PublishingSettings))
            setting.auto_publish_count = settings.auto_publish_max_per_hour - 1
            setting.auto_publish_window_started_at = utc_now()
            await db.commit()

            # The last slot of the current hour is still available.
            await reserve_automatic_slot(db, owner_id, "approve_all", revision)
            await db.commit()
            assert await _hourly_count(db, owner_id) == settings.auto_publish_max_per_hour

            # The window is full: the next automatic send is refused.
            with pytest.raises(HTTPException) as excinfo:
                await reserve_automatic_slot(db, owner_id, "approve_all", revision)
            assert excinfo.value.status_code == 429
            assert "per hour" in excinfo.value.detail["message"]

            # An hour later the same reservation succeeds and the count restarts.
            setting = await db.get(PublishingSettings, owner_id, populate_existing=True)
            setting.auto_publish_window_started_at = utc_now() - timedelta(minutes=61)
            await db.commit()
            await reserve_automatic_slot(db, owner_id, "approve_all", revision)
            await db.commit()
            assert await _hourly_count(db, owner_id) == 1
    run(main())


def test_hourly_rejection_does_not_spend_a_daily_token(workspace):
    """Regression: a staged daily token used to survive the hourly refusal."""
    set_mode(workspace, "approve_all")

    async def main():
        async with workspace.sessions() as db:
            owner_id, revision = await _owner_id(db), await _revision(db)
            setting = await db.scalar(select(PublishingSettings))
            setting.auto_publish_count = settings.auto_publish_max_per_hour
            setting.auto_publish_window_started_at = utc_now()
            await db.commit()
            key = f"auto_publish:day:{owner_id}"
            await _seed_counter(db, key, day_count=7)

            with pytest.raises(HTTPException) as excinfo:
                await reserve_automatic_slot(db, owner_id, "approve_all", revision)
            assert excinfo.value.status_code == 429

            row = await db.get(RateLimitCounter, key, populate_existing=True)
            assert row is not None and row.day_count == 7, (
                f"an hourly refusal must leave the daily budget alone, got {row.day_count}"
            )
    run(main())


def test_daily_budget_exhaustion_and_24h_rollover(workspace):
    """A spent day refuses; a day whose window elapsed starts counting again."""
    set_mode(workspace, "approve_all")

    async def main():
        async with workspace.sessions() as db:
            owner_id, revision = await _owner_id(db), await _revision(db)
            key = f"auto_publish:day:{owner_id}"
            await _seed_counter(db, key, day_count=settings.auto_publish_max_per_day)

            with pytest.raises(HTTPException) as excinfo:
                await reserve_automatic_slot(db, owner_id, "approve_all", revision)
            assert excinfo.value.status_code == 429
            assert "per day" in excinfo.value.detail["message"]

            row = await db.scalar(
                select(RateLimitCounter).where(RateLimitCounter.key == key)
            )
            row.day_started_at = utc_now() - timedelta(hours=24, seconds=1)
            await db.commit()
            await reserve_automatic_slot(db, owner_id, "approve_all", revision)
            await db.commit()
            row = await db.get(RateLimitCounter, key, populate_existing=True)
            assert row.day_count == 1
    run(main())


def test_minute_provider_window_rolls_over(workspace):
    async def main():
        async with workspace.sessions() as db:
            key = "llm:primary"
            # Only the minute window is at its limit; the day window has room.
            await _seed_counter(db, key,
                                window_count=settings.llm_requests_per_minute,
                                day_count=0)
            assert await job_queue.try_acquire(
                db, key, settings.llm_requests_per_minute, settings.llm_requests_per_day
            ) is False

            row = await db.get(RateLimitCounter, key, populate_existing=True)
            row.window_started_at = utc_now() - timedelta(seconds=61)
            await db.commit()
            assert await job_queue.try_acquire(
                db, key, settings.llm_requests_per_minute, settings.llm_requests_per_day
            ) is True
            row = await db.get(RateLimitCounter, key, populate_existing=True)
            assert row.window_count == 1, "an expired minute window restarts at one"
    run(main())


async def _exhaust(db, key, *, per_minute: int) -> None:
    row = await db.scalar(select(RateLimitCounter).where(RateLimitCounter.key == key))
    if row is None:
        db.add(RateLimitCounter(key=key, window_started_at=utc_now(),
                                window_count=per_minute, day_started_at=utc_now(),
                                day_count=0))
    else:
        row.window_count = per_minute
        row.window_started_at = utc_now()
    await db.commit()


async def _run_worker(workspace, worker_id):
    from app import worker
    async with workspace.sessions() as db:
        return await worker.run_once(worker_id, session=db)


def test_provider_budget_refusal_refunds_the_attempt_and_schedules_a_retry(
    workspace, pipeline
):
    """A refused provider call never happened, so the job keeps its retry budget."""
    set_mode(workspace, "approve_all")

    async def exhaust():
        async with workspace.sessions() as db:
            await _exhaust(db, "youtube:reads",
                           per_minute=settings.youtube_reads_per_minute)
    run(exhaust())

    enqueued = workspace.client.post(
        "/api/batch/jobs/enqueue", headers=workspace.headers,
        json={"video_id": "abcdefghijk", "comment_ids": ["c-budget"]},
    ).json()

    result = run(_run_worker(workspace, "budget-worker"))
    assert result["status"] == "retry", result

    async def job():
        async with workspace.sessions() as db:
            return await db.get(ReplyJob, enqueued["created"][0])
    job_row = run(job())
    assert job_row.status == "retry"
    # Claimed once (attempts=1) then refunded: the provider was never called.
    assert job_row.attempts == 0, job_row.attempts
    assert job_row.last_error["code"] == "youtube_reads_budget_exhausted"
    assert job_row.outcome.get("waiting_for") == "provider_budget"
    now = utc_now()
    next_run = job_row.next_run_at
    if next_run.tzinfo is None:
        delay = (next_run - now.replace(tzinfo=None)).total_seconds()
    else:
        delay = (next_run - now).total_seconds()
    assert 0 < delay <= job_queue.BUDGET_RETRY_SECONDS + 5, delay
    assert pipeline.published == []


def test_claim_next_honours_next_run_at(workspace):
    async def main():
        async with workspace.sessions() as db:
            owner_id = await _owner_id(db)
            owner = await db.get(ChannelOwner, owner_id)
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1"])
            job = await job_queue.claim_next(db, "worker-a")
            assert job is not None
            job_id = job.id
            # A classified delay pushes the job out of the ready window.
            await job_queue.release_for_retry(db, job_id, "worker-a",
                                              code="transient", delay_seconds=60)
            assert await job_queue.claim_next(db, "worker-b") is None, (
                "a job waiting on its backoff must not be claimable"
            )
            # Once the delay has passed it is claimable again.
            await db.execute(
                update(ReplyJob).where(ReplyJob.id == job_id)
                .values(next_run_at=utc_now() - timedelta(seconds=1))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            claimed = await job_queue.claim_next(db, "worker-b")
            assert claimed is not None and claimed.id == job_id
    run(main())


def test_backoff_delay_stays_inside_its_bounds():
    for attempts in range(1, 12):
        ceiling = min(30 * (2 ** (attempts - 1)), 900)
        for _ in range(50):
            delay = job_queue.backoff_delay(attempts)
            assert 1 <= delay <= ceiling, (attempts, delay, ceiling)
    # Deep in a retry loop the cap holds no matter how many attempts have happened.
    for _ in range(50):
        assert job_queue.backoff_delay(40) <= 900


def test_real_short_lease_expires_and_becomes_recoverable(workspace):
    """A genuinely short real window: one second of lease, waited for in real time."""
    async def main():
        async with workspace.sessions() as db:
            owner = await db.scalar(select(ChannelOwner))
            await job_queue.enqueue(db, owner, video_id="abcdefghijk", comment_ids=["c1"])
            job = await job_queue.claim_next(db, "worker-a", lease_seconds=1)
            assert job is not None
            job_id = job.id
            # A live lease still belongs to its worker, so the claim is refused.
            assert await job_queue.claim_next(db, "worker-b") is None
            await asyncio.sleep(1.2)
            result = await job_queue.recover_interrupted(db, lease_seconds=1)
            assert result["recovered"] == 1, result
            refreshed = await db.get(ReplyJob, job_id, populate_existing=True)
            assert refreshed.status == "pending"
    run(main())


@pytest.fixture
def pipeline(workspace, monkeypatch):
    published = []

    async def credentials_for_connection(connection, db):
        return object()

    async def insert(_credentials, parent_comment_id, text):
        published.append((parent_comment_id, text))
        return "reply-id"

    monkeypatch.setattr("app.services.job_pipeline.credentials_for_connection",
                        credentials_for_connection)
    monkeypatch.setattr("app.routers.publication._credentials",
                        lambda *a, **k: _async(object()))
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    workspace.published = published
    return workspace


async def _async(value):
    return value
