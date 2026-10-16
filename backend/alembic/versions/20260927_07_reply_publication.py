"""Add the durable record of a published reply.

This is a new table only: existing drafts, their reviews, their approval, and their
refinement conversations are untouched, and the live rows are carried over as they are.
The foreign key cascades, so permanently deleting a draft also deletes the record of
having posted it, which is the same rule the usage ledger already follows.

The partial unique index is the duplicate protection. YouTube's comments.insert takes no
idempotency key, so the database is the only place a second post can be refused. It
allows one in-flight, unresolved, or already public attempt per draft, while a
definitively rejected attempt, which created nothing, can be tried again on purpose.
"""
import sqlalchemy as sa

from alembic import op

revision = "20260927_07"
down_revision = "20260927_06"
branch_labels = None
depends_on = None

OPEN = "status in ('sending', 'unknown', 'posted')"


def upgrade():
    # A draft records a publication claim separately from the general operation token,
    # because an edit may supersede an in-flight review but must not supersede a
    # publish whose reply may already be public.
    op.add_column("reply_drafts", sa.Column("publication_token", sa.String(length=36),
                                            nullable=True))
    op.create_table(
        "reply_publications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("video_id", sa.String(length=64), nullable=False),
        sa.Column("parent_comment_id", sa.String(length=256), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("reply_comment_id", sa.String(length=256), nullable=True),
        sa.Column("failure", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["draft_id"], ["reply_drafts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("operation_id"),
    )
    op.create_index("ix_reply_publications_draft_id", "reply_publications", ["draft_id"])
    op.create_index("ix_reply_publications_status", "reply_publications", ["status"])
    op.create_index(
        "uq_publication_open_draft", "reply_publications", ["draft_id"],
        unique=True, postgresql_where=sa.text(OPEN), sqlite_where=sa.text(OPEN),
    )


def downgrade():
    op.drop_index("uq_publication_open_draft", table_name="reply_publications")
    op.drop_index("ix_reply_publications_status", table_name="reply_publications")
    op.drop_index("ix_reply_publications_draft_id", table_name="reply_publications")
    op.drop_table("reply_publications")
    op.drop_column("reply_drafts", "publication_token")
