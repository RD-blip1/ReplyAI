import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now


class ChannelOwner(Base):
    """The durable owner of a channel's retained drafts and usage history.

    Drafts used to hang off the browser session and the OAuth connection, both of which
    cascade, so an ordinary session expiry or logout destroyed the user's work. This row
    is the stable identity instead: it is created when a channel is first verified and
    survives credential removal, session expiry, and reconnection.

    It is reachable only through a channel that the server verified by completing OAuth
    and reading the authorized channel, never through a channel id supplied by a client.
    Deleting this row is the one action that destroys retained work, and only the
    explicit permanent-deletion action does that.
    """

    __tablename__ = "channel_owners"
    __table_args__ = (
        # Mirrors ix_channel_owners_channel_id created by the ownership migration, so the
        # model metadata and the migrated schema stay in agreement (alembic check).
        Index("ix_channel_owners_channel_id", "channel_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
