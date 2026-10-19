"""Durable background-job state for batch comment processing.

``ReplyJob`` is the queue: PostgreSQL (or the test SQLite database) is the only
authority on what is pending, running, or finished, so a worker crash, a restart, or a
lost connection never loses work. Workers hold a time-limited lease instead of a lock,
so an interrupted job becomes claimable again without anyone cleaning up after a dead
process.

Safety invariants, stated here because the schema enforces them:

- At most one *active* job per ``(channel_id, comment_id)`` (partial unique index).
  Duplicate ingestion and concurrent enqueue lose on that index, not on a
  read-then-write check.
- A job never publishes by itself. Publication always goes through the existing
  guarded path (``reply_publications`` claim + ``published_reply_guards``), which is
  the duplicate boundary for YouTube writes. A crash after the publication claim leaves
  the durable ``sending`` row, and job recovery reports ``unknown`` — it never retries
  the insert.
- Status transitions that matter are written with conditional UPDATEs (see
  ``app.services.job_queue``), so two workers cannot both observe a job as claimable.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now

# States a job can be waiting in. The partial unique index on (channel_id, comment_id)
# covers exactly these, so a finished job never blocks a later one for the same comment.
ACTIVE_JOB_STATUSES = ("pending", "retry", "processing", "verified", "publish_claimed")

# Claimable states: due when next_run_at has passed.
READY_JOB_STATUSES = ("pending", "retry")

# Terminal states. A terminal job records its reason in ``outcome`` and is never
# re-claimed.
TERMINAL_JOB_STATUSES = ("published", "review", "skipped", "failed", "unknown", "cancelled", "needs_reauth")


class ReplyJob(Base):
    """One queued comment-processing unit for one channel owner."""

    __tablename__ = "reply_jobs"
    __table_args__ = (
        Index(
            "uq_active_reply_job",
            "channel_id",
            "comment_id",
            unique=True,
            postgresql_where=text(
                "status in ('pending','retry','processing','verified','publish_claimed')"
            ),
            sqlite_where=text(
                "status in ('pending','retry','processing','verified','publish_claimed')"
            ),
        ),
        Index("ix_reply_jobs_claim", "status", "next_run_at"),
        Index("ix_reply_jobs_batch_id", "batch_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Owned like the drafts are: deleting the channel owner's retained work also
    # removes its queued and historical jobs.
    channel_owner_id: Mapped[str] = mapped_column(
        ForeignKey("channel_owners.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[str] = mapped_column(String(64))
    video_id: Mapped[str] = mapped_column(String(64))
    comment_id: Mapped[str] = mapped_column(String(256))
    # Audit pointer only, deliberately without a foreign key: deleting retained drafts
    # must not delete queued or historical job records.
    draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    # Crash-recovery passes. Recovering a job is not the same as a provider retry, so it
    # is counted separately and capped independently.
    recoveries: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Safe codes only, never provider payloads.
    last_error: Mapped[dict] = mapped_column(JSON, default=dict)
    # Structured final (or interim) decision: routing reason, publication view, etc.
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueueWorker(Base):
    """A live worker's heartbeat. Liveness is derived, never trusted from a request."""

    __tablename__ = "queue_workers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    jobs_processed: Mapped[int] = mapped_column(Integer, default=0)


class RateLimitCounter(Base):
    """Atomic shared budget for provider calls, keyed by bucket name.

    The conditional UPDATE in ``app.services.job_queue.try_acquire`` is the authority;
    this row is only storage. Both windows are rolling (first use starts the window),
    which is slightly stricter than calendar windows and never lets two concurrent
    workers both consume the last token.
    """

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    window_count: Mapped[int] = mapped_column(Integer, default=0)
    day_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    day_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
