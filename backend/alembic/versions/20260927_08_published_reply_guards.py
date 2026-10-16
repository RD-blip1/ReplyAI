"""Add the durable per-comment publication guard.

``reply_publications`` is scoped to a draft and cascades with it, so deleting a draft
also deletes the record of having posted to that comment. Once that record is gone,
YouTube's ``comments.insert`` offers nothing to stop a second reply: it takes no
idempotency key. This table keeps the minimum evidence that outlives the draft.

Existing successful posts are backfilled from ``reply_publications``. This is not
optional bookkeeping: a reply that was already published is a public fact, and a guard
table that started empty would leave every reply already on YouTube unprotected exactly
the moment its draft is deleted. Ambiguous attempts are backfilled too, so a request
whose outcome is genuinely unknown stays blocked rather than becoming retryable.

Only ``posted`` and ``unknown`` rows are copied. A definitively refused attempt created
nothing, so seeding a guard from it would turn a safe retry into a permanent block.

The unique index on ``(channel_id, parent_comment_id)`` is the duplicate boundary's
authority. Two requests reserving the same comment race on that index and exactly one
wins, which an application-level SELECT-then-INSERT could not guarantee.
"""
import uuid

import sqlalchemy as sa

from alembic import op

revision = "20260927_08"
down_revision = "20260927_07"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "published_reply_guards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("parent_comment_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("reply_comment_id", sa.String(length=256), nullable=True),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("publication_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # No foreign key to reply_drafts or channel_owners, on purpose: this row has to
    # outlive both.
    op.create_index(
        "uq_published_reply_guard", "published_reply_guards",
        ["channel_id", "parent_comment_id"], unique=True,
    )
    op.create_index(
        "ix_published_reply_guards_channel_id", "published_reply_guards", ["channel_id"],
    )

    # Carry over every attempt that is either public or genuinely uncertain. The draft id
    # is copied for audit only. Rows are skipped when a guard already covers the comment,
    # which keeps this safe to re-run and safe against two attempts on one comment where
    # the earlier one already represents it.
    connection = op.get_bind()
    existing = {
        (row.channel_id, row.parent_comment_id)
        for row in connection.execute(sa.text(
            "SELECT channel_id, parent_comment_id FROM published_reply_guards"
        )).fetchall()
    }
    for row in connection.execute(sa.text(
        "SELECT id, channel_id, parent_comment_id, status, reply_comment_id, draft_id, "
        "created_at, updated_at FROM reply_publications "
        "WHERE status IN ('posted', 'unknown') ORDER BY created_at"
    )).fetchall():
        if (row.channel_id, row.parent_comment_id) in existing:
            continue
        existing.add((row.channel_id, row.parent_comment_id))
        connection.execute(sa.text(
            "INSERT INTO published_reply_guards (id, channel_id, parent_comment_id, "
            "status, reply_comment_id, draft_id, publication_id, created_at, updated_at) "
            "VALUES (:id, :channel_id, :parent_comment_id, :status, :reply_comment_id, "
            ":draft_id, :publication_id, :created_at, :updated_at)"
        ), {
            "id": uuid.uuid4().hex,
            "channel_id": row.channel_id,
            "parent_comment_id": row.parent_comment_id,
            "status": row.status,
            "reply_comment_id": row.reply_comment_id,
            "draft_id": row.draft_id,
            "publication_id": row.id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        })


def downgrade():
    op.drop_index("ix_published_reply_guards_channel_id",
                  table_name="published_reply_guards")
    op.drop_index("uq_published_reply_guard", table_name="published_reply_guards")
    op.drop_table("published_reply_guards")