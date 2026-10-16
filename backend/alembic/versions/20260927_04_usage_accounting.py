"""Record per-request accounting: attempt number, consumption, and discard state.

The unique attempt key also makes a retried usage insert idempotent, so replaying an
accounting write cannot double-count a request. Rows already present were written
before these columns existed and no ledger rows predate that migration, so the
backfill below is only a schema default for an empty table.
"""
import sqlalchemy as sa

from alembic import op

revision = "20260927_04"
down_revision = "20260927_03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reply_draft_usage",
                  sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("reply_draft_usage",
                  sa.Column("consumption", sa.String(length=10), nullable=False,
                            server_default="reported"))
    op.add_column("reply_draft_usage",
                  sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("reply_draft_usage",
                  sa.Column("operation_token", sa.String(length=36), nullable=False,
                            server_default=""))
    op.create_index("uq_usage_attempt", "reply_draft_usage",
                    ["draft_id", "operation_token", "stage", "attempt"], unique=True)


def downgrade():
    op.drop_index("uq_usage_attempt", table_name="reply_draft_usage")
    op.drop_column("reply_draft_usage", "operation_token")
    op.drop_column("reply_draft_usage", "applied")
    op.drop_column("reply_draft_usage", "consumption")
    op.drop_column("reply_draft_usage", "attempt")
