"""Per-channel LLM usage for the last 24 hours, plus a local estimate of replies remaining.

The usage summary and the token gauge were designed by teammate Hrishik1033 (PR #3,
commit 1a74aab, "Speedometer added and other checks"); they were reproduced here by hand
and extended with request counts and the replies-remaining estimate.

Everything is computed from ReplyAI's own ledger (``reply_draft_usage``), never from the
provider. The ceilings are configured locally (``LLM_*_TOKENS_PER_DAY``,
``LLM_REQUESTS_PER_DAY``) and are not the provider's quota: other applications sharing an
API key, and the provider's own reset schedule, are invisible here.
"""
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import simulation
from app.config import settings
from app.dependencies import get_current_session, get_db
from app.models import AppSession, DraftUsage, ReplyDraft
from app.routers.youtube import _owned_channel

router = APIRouter(prefix="/api/usage", tags=["usage"])
Db = Annotated[AsyncSession, Depends(get_db)]
Session = Annotated[AppSession, Depends(get_current_session)]

ROLES = ("primary", "secondary")
WINDOW = timedelta(hours=24)
MIN_HISTORY = 10   # completed replies needed before an estimate is shown
HISTORY = 50       # most recent completed replies used for the per-reply averages
NOT_ENOUGH = "Not enough usage history to estimate remaining replies."


def token_limit(role: str) -> int:
    return max(0, getattr(settings, f"llm_{role}_tokens_per_day"))


def _percent(used: int, limit: int) -> float:
    if limit <= 0:
        return 0.0
    return round(used / limit * 100, 1)


def _tokens():
    return func.coalesce(DraftUsage.input_tokens, 0) + func.coalesce(DraftUsage.output_tokens, 0)


async def _window_usage(db: AsyncSession, owner_id: str, since: datetime) -> dict:
    rows = (await db.execute(
        select(DraftUsage.role, func.coalesce(func.sum(_tokens()), 0), func.count(DraftUsage.id),
               func.coalesce(func.sum(case((DraftUsage.consumption == "unknown", 1), else_=0)), 0))
        .join(ReplyDraft, ReplyDraft.id == DraftUsage.draft_id)
        .where(ReplyDraft.channel_owner_id == owner_id, DraftUsage.created_at >= since)
        .group_by(DraftUsage.role))).all()
    return {role: {"tokens": int(t or 0), "requests": int(n or 0), "unknown": int(u or 0)}
            for role, t, n, u in rows}


async def per_reply_cost(db: AsyncSession, owner_id: str) -> dict:
    """Average tokens and requests per completed reply, per role, over recent history.

    A completed reply is a draft whose generation and secondary review both finished;
    all of its recorded requests count (regenerations and refinements included), because
    that is what a reply really cost. Drafts with any unreadable token count are left
    out of the token average (unknown is not zero) but still count for requests.
    """
    draft_ids = (await db.scalars(
        select(ReplyDraft.id)
        .where(ReplyDraft.channel_owner_id == owner_id, ReplyDraft.generation_status == "complete",
               ReplyDraft.review_status == "complete")
        .order_by(ReplyDraft.updated_at.desc()).limit(HISTORY))).all()
    if not draft_ids:
        return {"completed_replies": 0, "roles": {}}
    rows = (await db.execute(
        select(DraftUsage.draft_id, DraftUsage.role, func.coalesce(func.sum(_tokens()), 0),
               func.count(DraftUsage.id),
               func.coalesce(func.sum(case((DraftUsage.consumption == "unknown", 1), else_=0)), 0))
        .where(DraftUsage.draft_id.in_(draft_ids))
        .group_by(DraftUsage.draft_id, DraftUsage.role))).all()
    roles = {}
    for role in ROLES:
        mine = [r for r in rows if r[1] == role]
        known = [int(r[2]) for r in mine if not r[4]]
        roles[role] = {
            "tokens_per_reply": round(sum(known) / len(known), 1) if known else None,
            "requests_per_reply": round(sum(int(r[3]) for r in mine) / len(draft_ids), 2),
        }
    return {"completed_replies": len(draft_ids), "roles": roles}


def estimate_remaining(window: dict, cost: dict) -> dict:
    """Replies that fit in what is left of every configured ceiling (minimum over roles
    and over tokens/requests). ``None`` when there is too little history or no ceiling."""
    if cost["completed_replies"] < MIN_HISTORY:
        return {"replies": None, "reason": "not_enough_history", "message": NOT_ENOUGH,
                "completed_replies": cost["completed_replies"], "min_history": MIN_HISTORY}
    best, limiting = math.inf, None
    request_limit = max(0, settings.llm_requests_per_day)
    for role in ROLES:
        used = window.get(role, {"tokens": 0, "requests": 0})
        per = cost["roles"].get(role, {})
        options = []
        if token_limit(role) and per.get("tokens_per_reply"):
            options.append((max(0, token_limit(role) - used["tokens"]) / per["tokens_per_reply"], "tokens"))
        if request_limit and per.get("requests_per_reply"):
            options.append((max(0, request_limit - used["requests"]) / per["requests_per_reply"], "requests"))
        for value, resource in options:
            if value < best:
                best, limiting = value, {"role": role, "resource": resource}
    if limiting is None:
        return {"replies": None, "reason": "no_ceiling", "message": "No daily ceiling is configured.",
                "completed_replies": cost["completed_replies"], "min_history": MIN_HISTORY}
    return {"replies": int(best), "reason": None, "limiting": limiting,
            "completed_replies": cost["completed_replies"], "min_history": MIN_HISTORY,
            "message": "Local estimate from ReplyAI's own records; not a provider quota."}


@router.get("/summary")
async def summary(session: Session, db: Db):
    """Reported consumption over the rolling last 24 hours, by model role.

    Only counts the provider actually reported are summed. A request whose counts were
    unreadable stays unknown and is reported as ``unknown_requests``, not as zero.
    """
    owner = await _owned_channel(db, session)
    window = await _window_usage(db, owner.id, datetime.now(UTC) - WINDOW)
    request_limit = max(0, settings.llm_requests_per_day)
    roles = {}
    for role in ROLES:
        used = window.get(role, {"tokens": 0, "requests": 0, "unknown": 0})
        roles[role] = {"used": used["tokens"], "limit": token_limit(role),
                       "percent": _percent(used["tokens"], token_limit(role)),
                       "requests": used["requests"], "request_limit": request_limit,
                       "unknown_requests": used["unknown"]}
    headline = max(ROLES, key=lambda role: roles[role]["percent"])
    cost = await per_reply_cost(db, owner.id)
    return {
        "llm": {
            "used": roles[headline]["used"], "limit": roles[headline]["limit"],
            "percent": roles[headline]["percent"], "headline_role": headline, "roles": roles,
            "unknown_requests": sum(r["unknown_requests"] for r in roles.values()),
            "window_hours": int(WINDOW.total_seconds() // 3600),
        },
        "per_reply": cost,
        "estimate": estimate_remaining(window, cost),
        "simulation": simulation.enabled(),
    }
