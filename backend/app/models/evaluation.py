import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now


class DraftEvaluation(Base):
    """An isolated record of what the automated publishing guardrails decided during a Dry Run.

    This table proves the decision-making logic without leaking side effects into the real
    publication state or Youtube API.
    """

    __tablename__ = "draft_evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_owner_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    video_id: Mapped[str] = mapped_column(String(64), index=True)
    comment_id: Mapped[str] = mapped_column(String(256))
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("reply_drafts.id", ondelete="CASCADE"), index=True
    )
    draft_revision: Mapped[int] = mapped_column(Integer)
    policy_revision: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(64))  # e.g., "would_publish", "skipped_off_topic"
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

