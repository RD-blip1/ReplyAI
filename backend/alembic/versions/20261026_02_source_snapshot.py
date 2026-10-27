"""Keep source edit metadata with retained drafts; existing snapshots remain nullable."""
import sqlalchemy as sa

from alembic import op

revision = "20261026_02"
down_revision = "20261026_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reply_drafts", sa.Column("source_updated_at", sa.DateTime(timezone=True)))
    op.add_column("reply_drafts", sa.Column("source_text_sha256", sa.Text()))
    op.add_column("reply_drafts", sa.Column("source_author_display_name", sa.Text()))


def downgrade():
    op.drop_column("reply_drafts", "source_author_display_name")
    op.drop_column("reply_drafts", "source_text_sha256")
    op.drop_column("reply_drafts", "source_updated_at")
