"""Owner-scoped opt-in discovery controls and decision history."""
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import (
    AutomationDecision,
    AutomationSettings,
    DiscoveredComment,
    ReplyDraft,
    ReplyJob,
    ReplyPolicyAllowlist,
)
from app.models.session import utc_now
from app.routers.drafts import Csrf, Db, Session, fail
from app.routers.youtube import _owned_channel, _owned_connection
from app.services import job_queue, publishing
from app.services.google_oauth import YOUTUBE_COMMENT_READ_SCOPES

router = APIRouter(prefix="/api/automation", tags=["automation"])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    paused: StrictBool = False
    lookback_hours: int = Field(default=0, ge=0, le=72, strict=True)


def view(row):
    if row is None:
        return {"enabled": False, "paused": False, "lookback_hours": 0, "revision": 0,
                "enabled_at": None, "discovery_cursor": None, "last_poll_started_at": None,
                "last_poll_finished_at": None, "next_poll_at": None, "last_error": None}
    return {key: getattr(row, key) for key in view(None)}


def decision_view(row):
    return {c.name: getattr(row, c.name) for c in AutomationDecision.__table__.columns
            if c.name != "channel_owner_id"}


@router.get("")
async def get_settings(session: Session, db: Db):
    owner = await _owned_channel(db, session)
    return view(await db.get(AutomationSettings, owner.id))


@router.put("")
async def put_settings(body: SettingsUpdate, session: Csrf, db: Db):
    owner = await _owned_channel(db, session)
    if body.enabled:
        connection = await _owned_connection(db, session)
        if connection.reconnect_required or not YOUTUBE_COMMENT_READ_SCOPES.issubset(
                (connection.scopes or "").split()):
            fail(409, "needs_reauth", "Reconnect with YouTube comment permissions before enabling discovery.")
    row = await db.get(AutomationSettings, owner.id)
    if row is None:
        try:
            async with db.begin_nested():
                row = AutomationSettings(channel_owner_id=owner.id)
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = await db.get(AutomationSettings, owner.id, populate_existing=True)
    now = utc_now()
    values = {"enabled": body.enabled, "paused": body.paused,
              "lookback_hours": body.lookback_hours, "revision": row.revision + 1,
              "updated_at": now}
    if body.enabled and row.enabled_at is None:
        values.update(enabled_at=now, discovery_cursor=now - timedelta(hours=body.lookback_hours))
    if not body.enabled or body.paused:
        # Invalidate an outstanding read. Its response cannot enqueue after this commit.
        values.update(poll_lease_owner=None, poll_lease_expires_at=None)
    changed = await db.execute(update(AutomationSettings).where(
        AutomationSettings.channel_owner_id == owner.id,
        AutomationSettings.revision == row.revision,
    ).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        await db.rollback()
        fail(409, "automation_settings_changed", "Settings changed concurrently; reload and retry.")
    if not body.enabled:
        jobs = (await db.scalars(select(ReplyJob.id).where(
            ReplyJob.channel_owner_id == owner.id,
            ReplyJob.batch_id.like("discovery:%"), ReplyJob.status.in_(("pending", "retry")),
        ))).all()
        # Cancellation commits; persist disabling first so no poll can add more work.
        await db.commit()
        for job_id in jobs:
            await job_queue.cancel(db, owner.id, job_id)
    else:
        await db.commit()
    await db.refresh(row)
    return view(row)


@router.post("/poll", status_code=202)
async def poll_now(session: Csrf, db: Db):
    owner = await _owned_channel(db, session)
    result = await db.execute(update(AutomationSettings).where(
        AutomationSettings.channel_owner_id == owner.id,
        AutomationSettings.enabled.is_(True), AutomationSettings.paused.is_(False),
    ).values(next_poll_at=utc_now()).execution_options(synchronize_session=False))
    await db.commit()
    if result.rowcount != 1:
        fail(409, "automation_disabled", "Enable and resume discovery before requesting a poll.")
    return {"status": "scheduled"}


@router.get("/summary")
async def summary(session: Session, db: Db, window: Literal["24h"] = "24h"):
    owner = await _owned_channel(db, session)
    since = utc_now() - timedelta(hours=24)
    decisions = (await db.execute(select(AutomationDecision.decision_class,
                                         AutomationDecision.execution_result, func.count())
        .where(AutomationDecision.channel_owner_id == owner.id,
               AutomationDecision.created_at >= since)
        .group_by(AutomationDecision.decision_class, AutomationDecision.execution_result))).all()
    discovered = (await db.execute(select(DiscoveredComment.disposition, func.count())
        .where(DiscoveredComment.channel_owner_id == owner.id, DiscoveredComment.first_seen_at >= since)
        .group_by(DiscoveredComment.disposition))).all()
    return {"window": window, "decisions": [{"decision_class": c, "execution_result": e, "count": n}
                                             for c, e, n in decisions],
            "discovery": dict(discovered)}


@router.get("/activity")
async def activity(session: Session, db: Db, limit: int = Query(50, ge=1, le=200),
                   decision_class: str | None = Query(None, alias="class"),
                   before: datetime | None = None):
    owner = await _owned_channel(db, session)
    query = select(AutomationDecision).where(AutomationDecision.channel_owner_id == owner.id)
    if decision_class:
        query = query.where(AutomationDecision.decision_class == decision_class)
    if before:
        query = query.where(AutomationDecision.created_at < before)
    rows = await db.scalars(query.order_by(AutomationDecision.created_at.desc(),
                                          AutomationDecision.id.desc()).limit(limit))
    return {"items": [decision_view(row) for row in rows]}


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str, session: Session, db: Db):
    owner = await _owned_channel(db, session)
    row = await db.scalar(select(AutomationDecision).where(
        AutomationDecision.id == decision_id, AutomationDecision.channel_owner_id == owner.id))
    if row is None:
        fail(404, "decision_not_found", "Decision not found for this channel.")
    return decision_view(row)


class AllowlistUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domains: list[str] = Field(default_factory=list, max_length=200)
    url_prefixes: list[str] = Field(default_factory=list, max_length=200)
    contacts: list[str] = Field(default_factory=list, max_length=50)
    handles: list[str] = Field(default_factory=list, max_length=50)
    approved_statements: list[str] = Field(default_factory=list, max_length=50)
    revision: int = Field(ge=0, strict=True)


ALLOWLIST_KEYS = ("domains", "url_prefixes", "contacts", "handles", "approved_statements")


def allowlist_view(row):
    if row is None:
        return {**{key: [] for key in ALLOWLIST_KEYS}, "revision": 0}
    return {**{key: list(getattr(row, key) or []) for key in ALLOWLIST_KEYS}, "revision": row.revision}


@router.get("/allowlist")
async def get_allowlist(session: Session, db: Db):
    owner = await _owned_channel(db, session)
    return allowlist_view(await db.get(ReplyPolicyAllowlist, owner.id))


@router.put("/allowlist")
async def put_allowlist(body: AllowlistUpdate, session: Csrf, db: Db):
    owner = await _owned_channel(db, session)
    row = await db.get(ReplyPolicyAllowlist, owner.id)
    current = row.revision if row else 0
    if body.revision != current:
        fail(409, "allowlist_changed", "The allow-list changed; reload and retry.")
    values = {key: sorted({item.strip() for item in getattr(body, key) if item.strip()})
              for key in ALLOWLIST_KEYS}
    if row is None:
        row = ReplyPolicyAllowlist(channel_owner_id=owner.id, revision=1, updated_at=utc_now(), **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.revision, row.updated_at = current + 1, utc_now()
    await db.commit()
    return allowlist_view(row)


review_router = APIRouter(prefix="/api/review-queue", tags=["review"])
QUEUE_CLASSES = ("HUMAN_REVIEW", "POLICY_BLOCK")


def _resolution(draft, attempt):
    if draft is None:
        return "draft_deleted"
    if attempt is not None:
        return "published" if attempt.status == "posted" else "publication_attempted"
    if draft.human_decision in ("approved", "rejected"):
        return draft.human_decision
    return None


@review_router.get("")
async def review_queue(session: Session, db: Db, limit: int = Query(50, ge=1, le=200),
                       decision_class: Literal["HUMAN_REVIEW", "POLICY_BLOCK", "all"] = Query(
                           "all", alias="class"), before: datetime | None = None):
    """Unresolved items whose latest decision needs a person, newest first."""
    owner = await _owned_channel(db, session)
    rows = (await db.scalars(select(AutomationDecision).where(
        AutomationDecision.channel_owner_id == owner.id,
        AutomationDecision.draft_id.is_not(None),
    ).order_by(AutomationDecision.created_at.desc(), AutomationDecision.id.desc()))).all()
    latest = {}
    for row in rows:
        latest.setdefault(row.draft_id, row)
    items, changed = [], False
    for row in sorted(latest.values(), key=lambda r: (r.created_at, r.id), reverse=True):
        if row.resolved_at is not None or row.decision_class not in QUEUE_CLASSES:
            continue
        if decision_class != "all" and row.decision_class != decision_class:
            continue
        if before and row.created_at >= before:
            continue
        draft = await db.get(ReplyDraft, row.draft_id)
        resolved = _resolution(draft, await publishing.latest_attempt(db, row.draft_id) if draft else None)
        if resolved:
            # Resolved through the existing manual flow; record it and drop it.
            row.resolved_at, row.resolution, changed = utc_now(), resolved, True
            continue
        trace = row.trace or {}
        items.append({
            "decision_id": row.id, "draft_id": row.draft_id, "job_id": row.job_id,
            "video_id": row.video_id, "comment_id": row.comment_id,
            "comment": draft.original_comment, "reply": draft.text, "revision": draft.revision,
            "decision_class": row.decision_class, "decision_code": row.decision_code,
            "reasons": trace.get("reasons") or ([trace["reason"]] if trace.get("reason") else []),
            "primary_classification": draft.primary_classification,
            "secondary_classification": draft.secondary_classification,
            "agreement": draft.agreement, "quality": draft.quality,
            "policy": publishing.review_summary(draft)["policy"],
            "input_signals": (draft.triage or {}).get("input_signals"),
            "triage": {k: v for k, v in (draft.triage or {}).items() if k != "input_signals"},
            "created_at": row.created_at,
        })
        if len(items) >= limit:
            break
    if changed:
        await db.commit()
    return {"items": items}


@review_router.post("/{decision_id}/dismiss")
async def dismiss(decision_id: str, session: Csrf, db: Db):
    owner = await _owned_channel(db, session)
    row = await db.scalar(select(AutomationDecision).where(
        AutomationDecision.id == decision_id, AutomationDecision.channel_owner_id == owner.id))
    if row is None:
        fail(404, "decision_not_found", "Decision not found for this channel.")
    if row.resolved_at is None:
        row.resolved_at, row.resolution = utc_now(), "dismissed"
        await db.commit()
    return {"decision_id": row.id, "resolution": row.resolution}
