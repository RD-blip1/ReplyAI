"""Add browser sessions and per-session YouTube connections.

Revision ID: 20260926_01
Revises:
Create Date: 2026-09-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260926_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_sessions",
        sa.Column("session_hash", sa.String(length=64), primary_key=True),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending_state_hash", sa.String(length=64), nullable=True),
        sa.Column("pending_code_verifier", sa.Text(), nullable=True),
        sa.Column("pending_state_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_app_sessions_expires_at", "app_sessions", ["expires_at"])
    op.create_table(
        "youtube_connections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("channel_title", sa.String(length=256), nullable=False),
        sa.Column("channel_thumbnail_url", sa.Text(), nullable=True),
        sa.Column("uploads_playlist_id", sa.String(length=128), nullable=False),
        sa.Column("encrypted_tokens", sa.Text(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.String(length=512), nullable=False),
        sa.Column("reconnect_required", sa.Boolean(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_hash"],
            ["app_sessions.session_hash"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("session_hash", name="uq_youtube_connections_session_hash"),
    )


def downgrade() -> None:
    op.drop_table("youtube_connections")
    op.drop_index("ix_app_sessions_expires_at", table_name="app_sessions")
    op.drop_table("app_sessions")