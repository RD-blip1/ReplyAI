"""Owner opt-in, durable discovery deduplication and policy decision history."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now


class AutomationSettings(Base):
    __tablename__ = "automation_settings"
    __table_args__ = (CheckConstraint("lookback_hours >= 0 AND lookback_hours <= 72",
                                     name="ck_automation_lookback"),)
    channel_owner_id: Mapped[str] = mapped_column(
        ForeignKey("channel_owners.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovery_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lookback_hours: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    poll_lease_owner: Mapped[str | None] = mapped_column(String(64))
    poll_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[dict | None] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DiscoveredComment(Base):
    __tablename__ = "discovered_comments"
    __table_args__ = (
        Index("uq_discovered_comment", "channel_id", "comment_id", unique=True),
        Index("ix_discovered_owner_seen", "channel_owner_id", "first_seen_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_owner_id: Mapped[str] = mapped_column(ForeignKey("channel_owners.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(String(64))
    comment_id: Mapped[str] = mapped_column(String(256))
    video_id: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    disposition: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[str | None] = mapped_column(String(36))
    poll_id: Mapped[str] = mapped_column(String(36))


class AutomationDecision(Base):
    __tablename__ = "automation_decisions"
    __table_args__ = (Index("ix_decisions_owner_created", "channel_owner_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_owner_id: Mapped[str] = mapped_column(ForeignKey("channel_owners.id", ondelete="CASCADE"))
    draft_id: Mapped[str | None] = mapped_column(String(36))
    job_id: Mapped[str | None] = mapped_column(String(36))
    comment_id: Mapped[str] = mapped_column(String(256))
    video_id: Mapped[str] = mapped_column(String(64))
    decision_class: Mapped[str] = mapped_column(String(32))
    decision_code: Mapped[str] = mapped_column(String(64))
    execution_result: Mapped[str | None] = mapped_column(String(64))
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(64))
    flags: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(64))


class ReplyPolicyAllowlist(Base):
    """Creator-approved links, contacts and handles a reply may contain (reply-policy rules R2, R4, R5)."""
    __tablename__ = "reply_policy_allowlists"
    channel_owner_id: Mapped[str] = mapped_column(
        ForeignKey("channel_owners.id", ondelete="CASCADE"), primary_key=True)
    domains: Mapped[list] = mapped_column(JSON, default=list)
    url_prefixes: Mapped[list] = mapped_column(JSON, default=list)
    contacts: Mapped[list] = mapped_column(JSON, default=list)
    handles: Mapped[list] = mapped_column(JSON, default=list)
    approved_statements: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
