"""Add opt-in discovery settings, ingestion history and automation decisions."""
import sqlalchemy as sa

from alembic import op

revision = "20261026_01"
down_revision = "20261018_01"
branch_labels = None
depends_on = None


def timestamp(name, nullable=True):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def owner(primary=False):
    return sa.Column("channel_owner_id", sa.String(36),
                     sa.ForeignKey("channel_owners.id", ondelete="CASCADE"),
                     primary_key=primary, nullable=False)


def upgrade():
    op.create_table(
        "automation_settings", owner(primary=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        timestamp("enabled_at"), timestamp("discovery_cursor"),
        sa.Column("lookback_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("poll_lease_owner", sa.String(64)), timestamp("poll_lease_expires_at"),
        timestamp("last_poll_started_at"), timestamp("last_poll_finished_at"),
        timestamp("next_poll_at"), sa.Column("last_error", sa.JSON()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        timestamp("updated_at", False),
        sa.CheckConstraint("lookback_hours >= 0 AND lookback_hours <= 72",
                           name="ck_automation_lookback"),
    )
    op.create_table(
        "discovered_comments", sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("comment_id", sa.String(256), nullable=False),
        sa.Column("video_id", sa.String(64), nullable=False),
        timestamp("published_at", False), timestamp("first_seen_at", False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(36)), sa.Column("poll_id", sa.String(36), nullable=False),
    )
    op.create_index("uq_discovered_comment", "discovered_comments",
                    ["channel_id", "comment_id"], unique=True)
    op.create_index("ix_discovered_owner_seen", "discovered_comments",
                    ["channel_owner_id", "first_seen_at"])
    op.create_table(
        "automation_decisions", sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("draft_id", sa.String(36)), sa.Column("job_id", sa.String(36)),
        sa.Column("comment_id", sa.String(256), nullable=False),
        sa.Column("video_id", sa.String(64), nullable=False),
        sa.Column("decision_class", sa.String(32), nullable=False),
        sa.Column("decision_code", sa.String(64), nullable=False),
        sa.Column("execution_result", sa.String(64)),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False), timestamp("created_at", False),
        timestamp("resolved_at"), sa.Column("resolution", sa.String(64)),
    )
    op.create_index("ix_decisions_owner_created", "automation_decisions",
                    ["channel_owner_id", "created_at"])


def downgrade():
    op.drop_table("automation_decisions")
    op.drop_table("discovered_comments")
    op.drop_table("automation_settings")
