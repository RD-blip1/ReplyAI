"""Conservative comparison of authoritative source text and creator reply state."""
import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import select

from app.config import settings
from app.models import ReplyPublication
from app.services import job_queue, youtube_api


def normalized(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def text_hash(text):
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Result:
    kind: str
    reason: str
    snapshot_missing: bool = False
    creator_reply_ids: tuple[str, ...] = ()


def compare(draft, snapshot):
    old_time = getattr(draft, "source_updated_at", None)
    old_hash = getattr(draft, "source_text_sha256", None)
    missing = old_time is None or old_hash is None
    if old_time is not None:
        if old_time.tzinfo is None:
            old_time = old_time.replace(tzinfo=UTC)
        if snapshot.updated_at != old_time:
            return Result("edited", "comment_edited", missing)
    if text_hash(snapshot.text) != (old_hash or text_hash(draft.original_comment)):
        return Result("edited", "comment_edited", missing)
    return Result("unchanged", "snapshot_missing" if missing else "unchanged", missing)


async def read_budget(db, count=1):
    if not await job_queue.try_acquire(db, "youtube:reads", settings.youtube_reads_per_minute,
                                       0, count=count):
        raise youtube_api.YouTubeFailure("youtube_budget_exhausted", "Read budget unavailable.", 429)
    if not await job_queue.try_acquire(db, "youtube:units", 0,
                                       settings.youtube_units_per_day_budget, count=count):
        raise youtube_api.YouTubeFailure("youtube_budget_exhausted", "Read budget unavailable.", 429)


async def revalidate(connection, credentials, draft, db, *, budgeted=False):
    try:
        source = await youtube_api.get_owned_comment_snapshot(
            connection, credentials, draft.video_id, draft.comment_id,
            before_read=(lambda: read_budget(db)) if budgeted else None)
        result = compare(draft, source)
        if result.kind != "unchanged":
            return result
        if source.total_reply_count <= 0:
            return result
        known = set(await db.scalars(select(ReplyPublication.reply_comment_id).where(
            ReplyPublication.channel_id == draft.channel_id,
            ReplyPublication.reply_comment_id.is_not(None))))
        state = await youtube_api.thread_state(
            credentials, draft.comment_id, draft.channel_id, known,
            before_read=(lambda: read_budget(db)) if budgeted else None)
        if state["creator_replied"]:
            return Result("creator_replied", "creator_already_replied", result.snapshot_missing,
                          tuple(state["creator_reply_ids"]))
        if not state["complete"]:
            return Result("read_error", "thread_scan_incomplete", result.snapshot_missing)
        return result
    except youtube_api.YouTubeFailure as exc:
        if exc.code == "comment_not_returned":
            return Result("gone", "comment_deleted")
        return Result("read_error", exc.code)
