import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now


class ReplyDraft(Base):
    """A retained draft, owned by the channel it was written for.

    Access is decided by resolving the caller's verified channel to a
    ``channel_owners`` row, so a reconnect to the same channel restores the draft while
    a different channel, or a caller with no verified connection, cannot reach it.
    """

    __tablename__ = "reply_drafts"
    __table_args__ = (
        UniqueConstraint("channel_owner_id", "comment_id", name="uq_draft_comment"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Ownership is the channel, not the session or the credential that happened to be
    # present when the draft was written, so neither can cascade this draft away.
    channel_owner_id: Mapped[str] = mapped_column(
        ForeignKey("channel_owners.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(String(64))
    video_id: Mapped[str] = mapped_column(String(64))
    comment_id: Mapped[str] = mapped_column(String(256))
    original_comment: Mapped[str] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_text_sha256: Mapped[str | None] = mapped_column(Text)
    source_author_display_name: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    providers: Mapped[dict] = mapped_column(JSON, default=dict)
    primary_classification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    secondary_classification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    agreement: Mapped[str] = mapped_column(String(20), default="pending")
    triage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    quality: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    generation_status: Mapped[str] = mapped_column(String(20), default="pending")
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    failure: Mapped[dict] = mapped_column(JSON, default=dict)
    human_decision: Mapped[str] = mapped_column(String(20), default="pending")
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    operation_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Set while a publication attempt owns this draft. It is deliberately separate from
    # operation_token: an edit is allowed to supersede an in-flight review, because a
    # late review result is simply discarded, but it must not supersede a publish, whose
    # reply may already be public.
    publication_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DraftUsage(Base):
    """Append-only ledger of provider requests made for a draft.

    ``ReplyDraft.token_usage`` is a per-stage snapshot of the most recent call, which
    is overwritten by later operations. This table records actual provider consumption
    instead, one row per HTTP request, keyed by the operation, stage, role, model, and
    revision it belongs to. Accounting is written independently of the guarded
    draft-state update, so consumption survives a discarded or rolled back result.

    ``consumption`` is ``reported`` when the provider returned counts for the request
    and ``unknown`` when it returned nothing readable, which is not the same as zero
    and is not a claim that the request was billed. ``applied`` is false when a stale
    guard rejected the result, so a discarded call is never presented as saved work.
    The unique attempt key makes re-running the insert idempotent, so retrying the
    write cannot double-count a request.
    """

    __tablename__ = "reply_draft_usage"
    __table_args__ = (
        Index("uq_usage_attempt", "draft_id", "operation_token", "stage", "attempt", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("reply_drafts.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    operation_token: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(20))
    stage: Mapped[str] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consumption: Mapped[str] = mapped_column(String(10), default="reported")
    applied: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
