"""Reply-policy allow-list and accepted input languages; both additive."""
import sqlalchemy as sa

from alembic import op

revision = "20261026_03"
down_revision = "20261026_02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reply_policy_allowlists",
        sa.Column("channel_owner_id", sa.String(36),
                  sa.ForeignKey("channel_owners.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("domains", sa.JSON(), nullable=False),
        sa.Column("url_prefixes", sa.JSON(), nullable=False),
        sa.Column("contacts", sa.JSON(), nullable=False),
        sa.Column("handles", sa.JSON(), nullable=False),
        sa.Column("approved_statements", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("reply_preferences", sa.Column("accepted_input_languages", sa.JSON()))


def downgrade():
    op.drop_column("reply_preferences", "accepted_input_languages")
    op.drop_table("reply_policy_allowlists")
