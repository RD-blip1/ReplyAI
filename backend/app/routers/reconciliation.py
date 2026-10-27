"""Checking and resolving publication attempts whose outcome is unknown.

``POST /api/drafts/{id}/publication/check`` reads YouTube and records a candidate; it
never changes the attempt's status. ``POST /api/drafts/{id}/publication/reconcile`` is
the only way an unknown attempt becomes ``posted`` or ``failed``, and only by a person.
Neither endpoint ever calls ``comments.insert``.
"""
import hashlib
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

from app.models import ReplyJob
from app.models.session import utc_now
from app.routers.drafts import Csrf, Db, fail, owned
from app.routers.publication import settle
from app.routers.youtube import _credentials, _owned_connection
from app.services import publication_reconcile, publishing, youtube_api

router = APIRouter(prefix="/api/drafts", tags=["reconciliation"])


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["posted", "not_posted"]
    reply_comment_id: str | None = Field(default=None, min_length=1, max_length=256)


async def _unknown_attempt(db, draft_id):
    attempt = await publishing.open_attempt(db, draft_id)
    if attempt is None or attempt.status not in ("unknown", "sending"):
        fail(409, "nothing_to_reconcile", "This draft has no publication with an unknown outcome.")
    return attempt


def _actor(session) -> str:
    # A stable, non-reversible reference to the browser session that decided.
    return hashlib.sha256(session.session_hash.encode()).hexdigest()[:16]


@router.post("/{draft_id}/publication/check")
async def check_unknown(draft_id: str, session: Csrf, db: Db):
    draft = await owned(db, session, draft_id)
    attempt = await _unknown_attempt(db, draft.id)
    connection = await _owned_connection(db, session)
    if connection.channel_id != attempt.channel_id:
        fail(409, "connection_changed", "Reconnect the channel this reply was sent from.")
    credentials = await _credentials(connection, db)
    try:
        seen = await publication_reconcile.check(credentials, attempt)
    except youtube_api.YouTubeFailure as exc:
        fail(503, "reconciliation_read_failed", exc.message)
    attempt.failure = {**(attempt.failure or {}), "reconciliation": seen}
    await db.commit()
    return {"publication": publishing.view(attempt), "reconciliation": seen,
            "message": ("A matching reply exists. ReplyAI cannot prove this request created it."
                        if seen["state"] == "external_match_found" else
                        "No matching reply was seen. That does not prove nothing was posted.")}


@router.post("/{draft_id}/publication/reconcile")
async def reconcile(draft_id: str, body: Resolution, session: Csrf, db: Db):
    draft = await owned(db, session, draft_id)
    attempt = await _unknown_attempt(db, draft.id)
    record = {**(attempt.failure or {}).get("reconciliation", {}), "provenance": "human_reconciled",
              "resolution": body.resolution, "resolved_by": _actor(session),
              "resolved_at": utc_now().isoformat()}
    if body.resolution == "posted":
        candidates = [c["reply_comment_id"] for c in record.get("candidates", [])]
        reply_id = body.reply_comment_id or (candidates[0] if len(candidates) == 1 else None)
        if not reply_id:
            fail(422, "reply_comment_id_required", "Give the id of the reply that exists on YouTube.")
        await settle(db, attempt, "posted", reply_comment_id=reply_id,
                     failure={"reconciliation": record})
        job_status, outcome = "published", {"decision": "published", "reply_comment_id": reply_id,
                                            "decision_class": "AUTO_PUBLISH",
                                            "execution_result": "posted_human_reconciled"}
    else:
        # Releases the guard (deleted, as for a definitive refusal) so a person may retry.
        await settle(db, attempt, "failed", failure={
            "code": "human_reconciled_not_posted",
            "message": "A person confirmed this reply is not on YouTube.", "reconciliation": record})
        job_status, outcome = "review", {"decision": "human_reconciled_not_posted",
                                         "decision_class": "HUMAN_REVIEW",
                                         "execution_result": "not_posted_human_reconciled"}
    await db.execute(update(ReplyJob).where(ReplyJob.draft_id == draft.id, ReplyJob.status == "unknown")
                     .values(status=job_status, outcome=outcome, updated_at=utc_now())
                     .execution_options(synchronize_session=False))
    await db.commit()
    return {"publication": publishing.view(await publishing.latest_attempt(db, draft.id))}
