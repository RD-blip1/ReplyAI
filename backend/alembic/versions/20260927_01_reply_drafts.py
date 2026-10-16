"""Add local reply drafts without changing existing tables."""
import sqlalchemy as sa

from alembic import op

revision = "20260927_01"
down_revision = "20260926_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reply_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_hash", sa.String(64), sa.ForeignKey("app_sessions.session_hash", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("youtube_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("video_id", sa.String(64), nullable=False),
        sa.Column("comment_id", sa.String(256), nullable=False),
        sa.Column("original_comment", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("providers", sa.JSON(), nullable=False),
        sa.Column("primary_classification", sa.JSON(), nullable=True),
        sa.Column("secondary_classification", sa.JSON(), nullable=True),
        sa.Column("agreement", sa.String(20), nullable=False),
        sa.Column("quality", sa.JSON(), nullable=True),
        sa.Column("generation_status", sa.String(20), nullable=False),
        sa.Column("review_status", sa.String(20), nullable=False),
        sa.Column("failure", sa.JSON(), nullable=False),
        sa.Column("human_decision", sa.String(20), nullable=False),
        sa.Column("operation_token", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("connection_id", "comment_id", name="uq_draft_comment"),
    )
    op.create_index("ix_reply_drafts_session_hash", "reply_drafts", ["session_hash"])
    op.create_index("ix_reply_drafts_connection_id", "reply_drafts", ["connection_id"])


def downgrade():
    op.drop_table("reply_drafts")
