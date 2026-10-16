"""Add an append-only provider usage ledger per draft.

Existing ``reply_drafts.token_usage`` snapshots are left untouched and are not
backfilled into history: past per-operation counts cannot be reconstructed.
"""
import sqlalchemy as sa

from alembic import op

revision = "20260927_03"
down_revision = "20260927_02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reply_draft_usage",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["reply_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reply_draft_usage_draft_id", "reply_draft_usage", ["draft_id"])


def downgrade():
    op.drop_index("ix_reply_draft_usage_draft_id", table_name="reply_draft_usage")
    op.drop_table("reply_draft_usage")
