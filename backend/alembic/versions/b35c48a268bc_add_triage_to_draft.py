"""add_triage_to_draft

Revision ID: b35c48a268bc
Revises: 20261009_01
Create Date: 2026-10-09 13:21:43.003233
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b35c48a268bc'
down_revision: str | None = '20261009_01'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reply_drafts", sa.Column("triage", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("reply_drafts", "triage")
