"""Persist reply preferences for each verified channel owner."""
import sqlalchemy as sa

from alembic import op

revision = "20261008_01"
down_revision = "20260927_08"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reply_preferences",
        sa.Column("channel_owner_id", sa.String(length=36), nullable=False),
        sa.Column("tone", sa.String(length=20), nullable=False, server_default="Friendly"),
        sa.Column("reply_length", sa.String(length=20), nullable=False, server_default="Medium"),
        sa.Column("language", sa.String(length=20), nullable=False, server_default="English"),
        sa.Column("channel_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_owner_id"], ["channel_owners.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("channel_owner_id"),
    )
def downgrade():
    op.drop_table("reply_preferences")
