import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now

# A guard exists for the whole lifetime of one parent comment, so unlike a publication
# row it is never scoped to a draft and never cascades away with one.
GUARD_OPEN_STATUSES = ("sending", "unknown", "posted")


class PublishedReplyGuard(Base):
    """One ReplyAI reply claim against one YouTube parent comment, kept after the draft.

    ``reply_publications`` answers "what happened to this attempt", and it is scoped to a
    draft, so deleting the draft deletes the answer. That was good enough while the only
    thing that could be lost was a record, but the record of a successful post is the
    reason a second one must not happen: YouTube's ``comments.insert`` accepts no
    idempotency key, so once this row is gone ReplyAI has no way to know it already
    replied to that comment, and a fresh draft would post a duplicate.

    This table is therefore the durable half of the duplicate boundary, keyed by the
    external publication identity rather than by any local draft:

    ``(channel_id, parent_comment_id)``
        The channel that holds the grant, plus the comment being replied to. Two
        different ReplyAI owners on two different channels never collide even if a
        comment id were somehow reused, and one owner cannot reply twice to the same
        comment.

    The unique index on that pair is the authority, not a SELECT-then-INSERT check. Two
    requests for the same comment race to reserve this row and exactly one wins, even
    though neither can see the other's uncommitted work.

    Lifecycle, which is deliberately not the same as ``ReplyPublication.status``:

    ``sending``
        Reserved by ``claim()`` before the insert leaves, so the row exists while the
        request is in flight. A reader that meets this can only be looking at a request
        whose writer stopped, so it is reported as ``unknown``.
    ``posted``
        YouTube returned the reply's id, kept in ``reply_comment_id``. Kept forever.
    ``unknown``
        The request may or may not have created the reply. Kept, so the ambiguity cannot
        be deleted away into a fresh send.
    ``(absent)``
        The request was definitively refused and created nothing, so the reservation is
        released and a deliberate retry may reserve it again. A refusal is not evidence
        of a public reply and must not become a permanent block.

    Deliberately absent: the reply text. Nothing here is needed to prevent or explain a
    duplicate, and the point of keeping a row after its draft is deleted is to avoid
    retaining the user's content. ``draft_id`` and ``publication_id`` are opaque
    identifiers kept for audit, with no foreign key, so a deleted draft cannot cascade
    into this table.
    """

    __tablename__ = "published_reply_guards"
    __table_args__ = (
        Index("uq_published_reply_guard", "channel_id", "parent_comment_id", unique=True),
        Index("ix_published_reply_guards_channel_id", "channel_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # The YouTube channel whose grant created the reply, not the local owner row. This is
    # what survives deleting and recreating a local owner for the same channel.
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_comment_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="sending")
    reply_comment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Audit pointers only. No foreign keys: a cascade from reply_drafts is exactly what
    # this table exists to survive.
    draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    publication_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )