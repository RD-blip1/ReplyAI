import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.session import utc_now

if TYPE_CHECKING:
    from app.models.session import AppSession


class YouTubeConnection(Base):
    __tablename__ = "youtube_connections"
    __table_args__ = (
        UniqueConstraint("session_hash", name="uq_youtube_connections_session_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_hash: Mapped[str] = mapped_column(
        ForeignKey("app_sessions.session_hash", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_title: Mapped[str] = mapped_column(String(256), nullable=False)
    channel_thumbnail_url: Mapped[str | None] = mapped_column(Text)
    uploads_playlist_id: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str] = mapped_column(String(512), nullable=False)
    reconnect_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    session: Mapped["AppSession"] = relationship(back_populates="youtube_connection")