"""Bounded, lease-fenced comment discovery. Never calls a publication API."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import AutomationSettings, ChannelOwner, DiscoveredComment, ReplyDraft
from app.models.session import utc_now
from app.services import job_pipeline, job_queue, youtube_api
from app.services.google_oauth import YOUTUBE_COMMENT_READ_SCOPES


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def timestamp(value):
    try:
        return aware(datetime.fromisoformat(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def claim(db, owner_id, poll_id):
    now = utc_now()
    result = await db.execute(update(AutomationSettings).where(
        AutomationSettings.channel_owner_id == owner_id,
        AutomationSettings.enabled.is_(True), AutomationSettings.paused.is_(False),
        or_(AutomationSettings.next_poll_at.is_(None), AutomationSettings.next_poll_at <= now),
        or_(AutomationSettings.poll_lease_expires_at.is_(None),
            AutomationSettings.poll_lease_expires_at < now),
    ).values(poll_lease_owner=poll_id, poll_lease_expires_at=now + timedelta(seconds=60),
             last_poll_started_at=now).execution_options(synchronize_session=False))
    await db.commit()
    return result.rowcount == 1


async def ingest(db, owner, poll_id, items):
    """Dedup row and queued job commit together, fenced against disable/lease loss."""
    now = utc_now()
    fence = await db.execute(update(AutomationSettings).where(
        AutomationSettings.channel_owner_id == owner.id,
        AutomationSettings.poll_lease_owner == poll_id,
        AutomationSettings.poll_lease_expires_at > now,
        AutomationSettings.enabled.is_(True), AutomationSettings.paused.is_(False),
    ).values(poll_lease_expires_at=now + timedelta(seconds=60))
      .execution_options(synchronize_session=False))
    if fence.rowcount != 1:
        await db.rollback()
        return False
    config = await db.get(AutomationSettings, owner.id, populate_existing=True)
    cursor = aware(config.discovery_cursor)
    for item in items:
        published = timestamp(item.get("published_at"))
        if not published or not item.get("comment_id") or not item.get("video_id"):
            continue  # malformed upstream item cannot authorize work
        disposition = "queued"
        if item.get("author_channel_id") == owner.channel_id:
            disposition = "own_comment"
        elif cursor is None or published < cursor:
            disposition = "before_cursor"
        elif await db.scalar(select(ReplyDraft.id).where(
            ReplyDraft.channel_owner_id == owner.id,
            ReplyDraft.comment_id == item["comment_id"],
        )):
            disposition = "existing_draft"
        row = DiscoveredComment(channel_owner_id=owner.id, channel_id=owner.channel_id,
                                comment_id=item["comment_id"], video_id=item["video_id"],
                                published_at=published, disposition=disposition, poll_id=poll_id)
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            continue
        if disposition == "queued":
            result = await job_queue.enqueue(db, owner, video_id=item["video_id"],
                                              comment_ids=[item["comment_id"]],
                                              batch_id=f"discovery:{poll_id[:26]}", commit=False)
            row.job_id = next(iter(result["created"]), None)
            row.disposition = "queued" if row.job_id else "existing_job"
    await db.commit()
    return True


async def poll(db, owner_id, poll_id):
    owner = await db.get(ChannelOwner, owner_id)
    config = await db.get(AutomationSettings, owner_id, populate_existing=True)
    cursor = aware(config.discovery_cursor)
    newest = cursor
    previous_failures = (config.last_error or {}).get("attempts", 0)
    error = None
    exhausted = False
    pages = 0
    try:
        connection = await job_pipeline.connection_for(db, owner.channel_id)
        if connection is None or not set(YOUTUBE_COMMENT_READ_SCOPES).issubset((connection.scopes or "").split()):
            error = "needs_reauth"
        else:
            credentials = await youtube_api.credentials_for_connection(connection, db)
            page_token = None
            for _ in range(max(1, settings.discovery_max_pages_per_poll)):
                if not await job_queue.try_acquire(db, "youtube:reads",
                                                   per_minute=settings.youtube_reads_per_minute,
                                                   per_day=0):
                    error = "youtube_budget_exhausted"
                    break
                if not await job_queue.try_acquire(db, "youtube:units", per_minute=0,
                                                   per_day=settings.youtube_units_per_day_budget):
                    error = "youtube_budget_exhausted"
                    break
                page = await youtube_api.list_channel_comments(owner.channel_id, credentials, page_token)
                pages += 1
                if not await ingest(db, owner, poll_id, page["items"]):
                    return {"status": "lease_lost", "pages": pages}
                dates = [d for item in page["items"] if (d := timestamp(item.get("published_at")))]
                if dates:
                    newest = max([d for d in [newest, *dates] if d is not None])
                page_token = page.get("next_page_token")
                if not page_token or (cursor and dates and min(dates) < cursor):
                    exhausted = True
                    break
    except youtube_api.YouTubeFailure as exc:
        error = "needs_reauth" if exc.status_code in (401, 403) else "youtube_unavailable"
    except Exception:  # noqa: BLE001 - fail closed with a sanitized status
        # Never persist credential/provider details in dashboard status.
        await db.rollback()
        error = "discovery_unavailable"
    now = utc_now()
    failures = min(previous_failures + 1, 20) if error else 0
    delay = max(1, settings.discovery_poll_seconds)
    if error:
        delay = max(delay, job_queue.backoff_delay(failures))
    values = dict(  # noqa: C408
poll_lease_owner=None, poll_lease_expires_at=None,
                  last_poll_finished_at=now,
                  next_poll_at=now + timedelta(seconds=delay),
                  last_error={"code": error, "attempts": failures} if error else None)
    # A truncated listing is not a complete interval: advancing its watermark would
    # permanently hide unseen older comments. Retain it for the next bounded poll.
    if not error and exhausted and newest and cursor:
        values["discovery_cursor"] = max(cursor, newest - timedelta(minutes=5))
    await db.execute(update(AutomationSettings).where(
        AutomationSettings.channel_owner_id == owner_id,
        AutomationSettings.poll_lease_owner == poll_id,
    ).values(**values).execution_options(synchronize_session=False))
    await db.commit()
    return {"status": error or "polled", "pages": pages}


async def run_due_once(db_factory):
    async with db_factory() as db:
        now = utc_now()
        owners = (await db.scalars(select(AutomationSettings.channel_owner_id).where(
            AutomationSettings.enabled.is_(True), AutomationSettings.paused.is_(False),
            or_(AutomationSettings.next_poll_at.is_(None), AutomationSettings.next_poll_at <= now),
        ).order_by(AutomationSettings.next_poll_at.asc()).limit(20))).all()
        for owner_id in owners:
            poll_id = str(uuid.uuid4())
            if await claim(db, owner_id, poll_id):
                return await poll(db, owner_id, poll_id)
    return {"status": "idle"}
