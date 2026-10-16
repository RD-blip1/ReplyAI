"""Record provider-reported token usage per draft stage without changing existing tables."""
import sqlalchemy as sa

from alembic import op

revision = "20260927_02"
down_revision = "20260927_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "reply_drafts",
        sa.Column("token_usage", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade():
    op.drop_column("reply_drafts", "token_usage")
