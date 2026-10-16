from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.youtube_connection import YouTubeConnection


def utc_now() -> datetime:
    return datetime.now(UTC)


class AppSession(Base):
    __tablename__ = "app_sessions"

    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    pending_state_hash: Mapped[str | None] = mapped_column(String(64))
    pending_code_verifier: Mapped[str | None] = mapped_column(Text)
    pending_state_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    youtube_connection: Mapped["YouTubeConnection | None"] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )