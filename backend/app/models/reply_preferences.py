from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now


class ReplyPreferences(Base):
    __tablename__ = "reply_preferences"

    channel_owner_id: Mapped[str] = mapped_column(
        ForeignKey("channel_owners.id", ondelete="CASCADE"), primary_key=True
    )
    tone: Mapped[str] = mapped_column(String(20), nullable=False, default="Friendly")
    reply_length: Mapped[str] = mapped_column(String(20), nullable=False, default="Medium")
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="English")
    channel_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Languages ReplyAI understands as input; distinct from the reply language.
    accepted_input_languages: Mapped[list | None] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
