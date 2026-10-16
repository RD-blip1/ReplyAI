import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session import utc_now

# A draft may hold only one publication that is in flight, unresolved, or already
# public. The condition is partial so that a definitively rejected attempt, which
# posted nothing, can be tried again, while a successful one blocks the draft for
# good and an uncertain one blocks it until a person checks YouTube.
OPEN_PUBLICATION_STATUSES = ("sending", "unknown", "posted")


class ReplyPublication(Base):
    """One attempt to publish an approved draft as a public reply on YouTube.

    The row is committed before the insert is sent and carries a frozen copy of
    everything the request was built from: the channel, the video, the comment being
    replied to, the draft revision, and the exact text. Nothing here is edited
    afterwards, so the record still describes the request even if the draft is later
    changed or the reply is later deleted on YouTube.

    For a claimed send, ``operation_id`` is the token written to the draft's
    ``operation_token`` while the attempt runs, which makes the claim and attempt one
    atomic fact rather than two things that can disagree after a crash. Pre-send
    automatic failures have an audit id but do not claim the draft or reserve a comment.
    YouTube's ``comments.insert`` accepts no idempotency key, so the claimed record and
    durable guard are the duplicate protection.

    ``status`` records what is actually known:

    ``sending``
        Committed before the request left. A reader that sees this can only be looking
        at a request whose writer stopped, so it is reported as ``unknown``.
    ``posted``
        YouTube returned the created reply, whose id is kept in ``reply_comment_id``.
    ``failed``
        YouTube definitively rejected a send, or an automatic preflight did not send
        because mode, permission, or rate limits blocked it. Nothing was posted, so
        this is the only state a person may deliberately try again.
    ``unknown``
        The request may or may not have been created: a timeout, a server error, a
        connection that broke, or a database failure after the provider acted. It is
        never retried automatically, because a repeat risks a duplicate public reply.
    """

    __tablename__ = "reply_publications"
    __table_args__ = (
        Index(
            "uq_publication_open_draft",
            "draft_id",
            unique=True,
            postgresql_where=text("status in ('sending', 'unknown', 'posted')"),
            sqlite_where=text("status in ('sending', 'unknown', 'posted')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("reply_drafts.id", ondelete="CASCADE"), index=True)
    # The idempotency key. Unique, so one operation can never own two attempts.
    operation_id: Mapped[str] = mapped_column(String(36), unique=True)
    channel_id: Mapped[str] = mapped_column(String(64))
    video_id: Mapped[str] = mapped_column(String(64))
    # The draft's top-level comment, which is what YouTube expects as parentId.
    parent_comment_id: Mapped[str] = mapped_column(String(256))
    draft_revision: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="sending", index=True)
    reply_comment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Only the safe code and message. Provider payloads and tokens are never stored.
    failure: Mapped[dict] = mapped_column(JSON, default=dict)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
