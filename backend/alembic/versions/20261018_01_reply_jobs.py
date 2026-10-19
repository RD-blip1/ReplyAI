"""Durable reply-job queue, worker heartbeats, and shared rate-limit counters.

Additive only: three new tables and their indexes. Nothing existing is altered, so the
migration is safe to apply to a database with live data after the standard
upgrade/downgrade verification on a disposable database.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261018_01"
down_revision = "850fa922f687"
branch_labels = None
depends_on = None

_ACTIVE = "('pending','retry','processing','verified','publish_claimed')"


def upgrade():
    op.create_table(
        "reply_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel_owner_id", sa.String(length=36), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("video_id", sa.String(length=64), nullable=False),
        sa.Column("comment_id", sa.String(length=256), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("recoveries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("outcome", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["channel_owner_id"], ["channel_owners.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reply_jobs_channel_owner_id", "reply_jobs", ["channel_owner_id"])
    op.create_index("ix_reply_jobs_draft_id", "reply_jobs", ["draft_id"])
    op.create_index("ix_reply_jobs_status", "reply_jobs", ["status"])
    op.create_index("ix_reply_jobs_claim", "reply_jobs", ["status", "next_run_at"])
    op.create_index("ix_reply_jobs_batch_id", "reply_jobs", ["batch_id"])
    op.create_index(
        "uq_active_reply_job",
        "reply_jobs",
        ["channel_id", "comment_id"],
        unique=True,
        postgresql_where=sa.text(f"status in {_ACTIVE}"),
        sqlite_where=sa.text(f"status in {_ACTIVE}"),
    )

    op.create_table(
        "queue_workers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("jobs_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("day_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("day_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade():
    op.drop_table("rate_limit_counters")
    op.drop_table("queue_workers")
    op.drop_table("reply_jobs")
