from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now


class PublishingSettings(Base):
    """Durable publishing policy and automatic-send rate window for one channel."""

    __tablename__ = "publishing_settings"

    channel_owner_id: Mapped[str] = mapped_column(
        ForeignKey("channel_owners.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_review")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auto_publish_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_publish_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
