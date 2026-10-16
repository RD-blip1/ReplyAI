"""Persist channel-scoped publishing modes and automatic-send rate limits."""
import sqlalchemy as sa

from alembic import op

revision = "20261009_01"
down_revision = "20261008_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publishing_settings",
        sa.Column("channel_owner_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="manual_review"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("auto_publish_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto_publish_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_owner_id"], ["channel_owners.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("channel_owner_id"),
    )


def downgrade():
    op.drop_table("publishing_settings")
