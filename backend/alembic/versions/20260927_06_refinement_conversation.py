"""Add the reply-refinement conversation, stored per draft.

Turns are new records and nothing existing is altered: drafts keep their columns, their
ownership, and their usage history. The foreign key cascades so that permanently deleting
a draft also deletes its conversation, while a session expiry, a disconnect, or a logout
leaves the conversation intact alongside the draft it belongs to.
"""
import sqlalchemy as sa

from alembic import op

revision = "20260927_06"
down_revision = "20260927_05"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "refinement_turns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("operation_token", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["draft_id"], ["reply_drafts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("operation_token"),
    )
    op.create_index("ix_refinement_turns_draft_id", "refinement_turns", ["draft_id"])
    op.create_index("ix_refinement_turns_status", "refinement_turns", ["status"])
    op.create_index("ix_refinement_turns_draft_created", "refinement_turns",
                    ["draft_id", "created_at"])


def downgrade():
    op.drop_index("ix_refinement_turns_draft_created", table_name="refinement_turns")
    op.drop_index("ix_refinement_turns_status", table_name="refinement_turns")
    op.drop_index("ix_refinement_turns_draft_id", table_name="refinement_turns")
    op.drop_table("refinement_turns")
