"""add_draft_evaluations

Revision ID: 850fa922f687
Revises: b35c48a268bc
Create Date: 2026-10-09 17:01:40.432260
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '850fa922f687'
down_revision: str | None = 'b35c48a268bc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'draft_evaluations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('channel_owner_id', sa.String(length=64), nullable=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False),
        sa.Column('video_id', sa.String(length=64), nullable=False),
        sa.Column('comment_id', sa.String(length=256), nullable=False),
        sa.Column('draft_id', sa.String(length=36), nullable=False),
        sa.Column('draft_revision', sa.Integer(), nullable=False),
        sa.Column('policy_revision', sa.Integer(), nullable=False),
        sa.Column('decision', sa.String(length=64), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['draft_id'], ['reply_drafts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_draft_evaluations_channel_id'), 'draft_evaluations', ['channel_id'], unique=False)
    op.create_index(op.f('ix_draft_evaluations_channel_owner_id'), 'draft_evaluations', ['channel_owner_id'], unique=False)
    op.create_index(op.f('ix_draft_evaluations_draft_id'), 'draft_evaluations', ['draft_id'], unique=False)
    op.create_index(op.f('ix_draft_evaluations_video_id'), 'draft_evaluations', ['video_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_draft_evaluations_video_id'), table_name='draft_evaluations')
    op.drop_index(op.f('ix_draft_evaluations_draft_id'), table_name='draft_evaluations')
    op.drop_index(op.f('ix_draft_evaluations_channel_owner_id'), table_name='draft_evaluations')
    op.drop_index(op.f('ix_draft_evaluations_channel_id'), table_name='draft_evaluations')
    op.drop_table('draft_evaluations')
