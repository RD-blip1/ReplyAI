import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now


class RefinementTurn(Base):
    """One exchange of the reply-refinement conversation for a draft.

    A turn records what the user asked for, what the model proposed, and the draft
    revision that proposal was written against. The proposal is kept here rather than
    written into the draft so that generating a suggestion never disturbs the saved
    reply, its approval, or its review; only an explicit apply does that, and only when
    the saved revision still matches ``base_revision``.

    Turns hang off the draft, so they follow the draft's durable channel ownership: they
    survive a refresh and an authorized same-channel reconnection, and they are removed
    when the draft is permanently deleted.
    """

    __tablename__ = "refinement_turns"
    __table_args__ = (Index("ix_refinement_turns_draft_created", "draft_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("reply_drafts.id", ondelete="CASCADE"), index=True)
    base_revision: Mapped[int] = mapped_column(Integer)
    instruction: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="suggested", index=True)
    operation_token: Mapped[str] = mapped_column(String(36), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
