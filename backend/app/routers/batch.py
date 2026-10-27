"""Batch job management API.

Ownership is never asserted by the client: the owner is resolved from the caller's
verified YouTube connection (``_owned_channel``), exactly like the draft routes, so a
job id alone cannot reach another channel's work.

This router only reads and writes the durable job table. It never calls YouTube and
never generates or publishes anything; the worker process does that. Enqueue relies on
the partial unique index for duplicate refusal, so concurrent submissions lose on the
database rather than on a read-then-write check.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.models import ReplyJob, ReplyPublication
from app.models.session import utc_now
from app.routers.drafts import Csrf, Db, Session, fail
from app.routers.youtube import _owned_channel
from app.services import job_queue

router = APIRouter(prefix="/api/batch/jobs", tags=["batch-jobs"])


class EnqueueRequest(BaseModel):
    video_id: str = Field(min_length=1, max_length=64)
    comment_ids: list[str] = Field(min_length=1, max_length=100)
    batch_id: str | None = Field(default=None, min_length=8, max_length=36)
    max_attempts: int = Field(default=3, ge=1, le=10)
    priority: int = Field(default=0, ge=-100, le=100)


class EnqueueResponse(BaseModel):
    created: list[str]
    duplicates: int
    batch_id: str


class JobView(BaseModel):
    id: str
    batch_id: str | None = None
    channel_id: str
    video_id: str
    comment_id: str
    draft_id: str | None = None
    status: str
    attempts: int
    max_attempts: int
    recoveries: int
    last_error: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] = Field(default_factory=dict)
    next_run_at: str | None = None
    lease_expires_at: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


class ListResponse(BaseModel):
    items: list[JobView]
    total: int


class ReconcileResponse(BaseModel):
    recovered: int
    marked_unknown: int
    marked_published: int
    failed: int


async def _owned_job(db, session, job_id: str) -> ReplyJob:
    owner = await _owned_channel(db, session)
    job = await db.scalar(
        select(ReplyJob).where(
            ReplyJob.id == job_id, ReplyJob.channel_owner_id == owner.id
        )
    )
    if job is None:
        fail(404, "job_not_found", "Job not found for this channel.")
    return job


@router.post("/enqueue", response_model=EnqueueResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_jobs(payload: EnqueueRequest, session: Csrf, db: Db) -> EnqueueResponse:
    owner = await _owned_channel(db, session)
    batch_id = payload.batch_id or str(uuid.uuid4())
    result = await job_queue.enqueue(
        db,
        owner,
        video_id=payload.video_id,
        comment_ids=payload.comment_ids,
        batch_id=batch_id,
        max_attempts=payload.max_attempts,
        priority=payload.priority,
    )
    return EnqueueResponse(
        created=result["created"], duplicates=result["duplicates"], batch_id=batch_id
    )


@router.get("/stats/summary")
async def stats_summary(session: Session, db: Db) -> dict[str, Any]:
    owner = await _owned_channel(db, session)
    return await job_queue.queue_stats(db, owner.id)


@router.post("/reconcile", response_model=ReconcileResponse)
async def reconcile_jobs(
    session: Csrf, db: Db, lease_seconds: int = 120, max_recoveries: int = 3
) -> ReconcileResponse:
    owner = await _owned_channel(db, session)
    res = await job_queue.recover_interrupted(
        db, lease_seconds=lease_seconds, max_recoveries=max_recoveries, owner_id=owner.id
    )
    return ReconcileResponse(**res)


@router.get("", response_model=ListResponse)
async def list_jobs(
    session: Session,
    db: Db,
    limit: int = Query(default=50, ge=1, le=200),
    status_filter: str | None = Query(default=None),
) -> ListResponse:
    owner = await _owned_channel(db, session)
    stmt = select(ReplyJob).where(ReplyJob.channel_owner_id == owner.id)
    if status_filter:
        stmt = stmt.where(ReplyJob.status == status_filter)
    stmt = stmt.order_by(ReplyJob.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    total = await db.scalar(
        select(func.count()).select_from(ReplyJob).where(ReplyJob.channel_owner_id == owner.id)
    )
    return ListResponse(items=[JobView(**job_queue.view(r)) for r in rows], total=total or 0)


@router.get("/{job_id}", response_model=JobView)
async def get_job(job_id: str, session: Session, db: Db) -> JobView:
    return JobView(**job_queue.view(await _owned_job(db, session, job_id)))


@router.post("/{job_id}/cancel", response_model=JobView)
async def cancel_job(job_id: str, session: Csrf, db: Db) -> JobView:
    owner = await _owned_channel(db, session)
    job = await job_queue.cancel(db, owner.id, job_id)
    if job is None:
        fail(409, "cannot_cancel", "Job cannot be cancelled (already running or finished).")
    return JobView(**job_queue.view(job))


@router.post("/{job_id}/requeue", response_model=JobView)
async def requeue_job(job_id: str, session: Csrf, db: Db) -> JobView:
    """Reset a finished job to ``pending`` (administrative override).

    ``unknown`` publication outcomes are requeueable here only because a person is
    explicitly asking; the worker itself never retries them.
    """
    owner = await _owned_channel(db, session)
    job = await _owned_job(db, session, job_id)
    requeueable = ("failed", "review", "skipped", "cancelled", "needs_reauth", "unknown")
    if job.status not in requeueable:
        fail(409, "cannot_requeue", f"Cannot requeue a job in status {job.status}.")
    now = utc_now()
    result = await db.execute(
        update(ReplyJob)
        .where(
            ReplyJob.id == job.id,
            ReplyJob.channel_owner_id == owner.id,
            ReplyJob.status.in_(requeueable),
        )
        .values(
            status="pending",
            attempts=0,
            recoveries=0,
            next_run_at=now,
            lease_expires_at=None,
            worker_id=None,
            last_error={},
            outcome={},
            finished_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        fail(409, "requeue_conflict", "Job changed concurrently.")
    await db.refresh(job)
    return JobView(**job_queue.view(job))


@router.get("/{job_id}/publication")
async def job_publication(job_id: str, session: Session, db: Db) -> dict[str, Any]:
    job = await _owned_job(db, session, job_id)
    if not job.draft_id:
        return {"publication": None}
    attempt = await db.scalar(
        select(ReplyPublication)
        .where(ReplyPublication.draft_id == job.draft_id)
        .order_by(ReplyPublication.created_at.desc(), ReplyPublication.id.desc())
    )
    if attempt is None:
        return {"publication": None}
    return {"publication": {
        "status": attempt.status,
        "reply_comment_id": attempt.reply_comment_id,
        "draft_revision": attempt.draft_revision,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
    }}
