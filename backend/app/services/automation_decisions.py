"""Durable decision history: one ``automation_decisions`` row per automatic decision.

Writes use their own short session so a rollback in the caller (the worker routinely
rolls back on conflicts) can neither lose the record nor be affected by it. Recording
is an audit trail: a failure to record is logged and never changes what was published.
"""
import logging
from types import SimpleNamespace

from sqlalchemy import select, update

from app import database
from app.config import settings
from app.models import AutomationDecision, ReplyDraft, ReplyJob
from app.services import automation_policy, reply_policy

logger = logging.getLogger(__name__)

FLAG_NAMES = ("input_signals_mode", "reply_policy_mode", "grounding_variant", "language_contract",
              "triage_skip_threshold", "revalidate_before_publish", "stage_pipeline_shared",
              "routing_mode", "llm_budget_mode")


def flags() -> dict:
    return {name: getattr(settings, name) for name in FLAG_NAMES}


def trace_for(draft: ReplyDraft | None, *, result: dict | None = None, extra: dict | None = None) -> dict:
    """What a person needs to audit the decision: every stage, label and check."""
    trace = dict(extra or {})
    if draft is not None:
        triage = dict(draft.triage or {})
        signals = triage.pop("input_signals", None)
        trace.update({
            "comment": draft.original_comment,
            "reply": draft.text,
            "draft_revision": draft.revision,
            "providers": draft.providers,
            "triage": triage or None,
            "input_signals": signals,
            "primary_classification": draft.primary_classification,
            "secondary_classification": draft.secondary_classification,
            "agreement": draft.agreement,
            "quality": draft.quality,
            "stage_failures": {k: v for k, v in (draft.failure or {}).items() if not k.startswith("_")},
            "policy": [v.as_dict() for v in reply_policy.check(
                draft.text or "", comment=draft.original_comment or "",
                commenter_handle=draft.source_author_display_name)] if draft.text else [],
        })
    if result is not None:
        trace["publication"] = {k: result.get(k) for k in
                                ("mode", "decision", "reason", "attempt_status") if k in result}
        published = result.get("published") or {}
        if isinstance(published, dict) and published.get("publication"):
            trace["publication"]["status"] = published["publication"].get("status")
    return trace


async def record(*, owner_id: str, comment_id: str, video_id: str, decision_class: str,
                 decision_code: str, execution_result: str | None, draft_id: str | None = None,
                 job_id: str | None = None, trace: dict | None = None) -> str | None:
    try:
        async with database.sessions()() as db:
            row = AutomationDecision(
                channel_owner_id=owner_id, draft_id=draft_id, job_id=job_id,
                comment_id=comment_id, video_id=video_id, decision_class=decision_class,
                decision_code=(decision_code or "unknown")[:64],
                execution_result=execution_result, trace=_jsonable(trace or {}),
                policy_version=automation_policy.POLICY_VERSION, flags=flags())
            db.add(row)
            await db.commit()
            return row.id
    except Exception:
        logger.exception("event=decision_record_failed owner=%s comment=%s", owner_id, comment_id)
        return None


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


SNAPSHOT_FIELDS = ("id", "channel_owner_id", "comment_id", "video_id", "original_comment", "text",
                   "revision", "providers", "triage", "primary_classification",
                   "secondary_classification", "agreement", "quality", "failure",
                   "source_author_display_name")


def snapshot(draft: ReplyDraft) -> SimpleNamespace:
    """Plain copy taken before publication; a rollback there expires ORM attributes."""
    return SimpleNamespace(**{name: getattr(draft, name) for name in SNAPSHOT_FIELDS})


async def record_ui_result(draft, result: dict | None) -> None:
    """The synchronous Generate path's automatic decision (modes other than manual)."""
    if not result or "decision_class" not in result:
        return
    await record(owner_id=draft.channel_owner_id, comment_id=draft.comment_id,
                 video_id=draft.video_id, draft_id=draft.id,
                 decision_class=result["decision_class"], decision_code=result["decision_code"],
                 execution_result=result.get("execution_result"),
                 trace=trace_for(draft, result=result, extra={"origin": "ui",
                                                              "reasons": result.get("reasons")}))


async def record_job(job_id: str, status: str) -> None:
    """Record a worker job's decision and stamp the class into ``job.outcome``."""
    if status in ("lost", "unreachable"):
        return
    try:
        async with database.sessions()() as db:
            job = await db.get(ReplyJob, job_id)
            if job is None:
                return
            outcome = dict(job.outcome or {})
            code = (outcome.get("decision_code") or (job.last_error or {}).get("code")
                    or outcome.get("decision") or status)
            cls = outcome.get("decision_class") or automation_policy.class_for_job(status, code)
            execution = outcome.get("execution_result") or automation_policy.execution_result(status, code)
            draft = await db.get(ReplyDraft, job.draft_id) if job.draft_id else None
            if status != "retry":
                # Terminal jobs carry their class; a retry may be re-claimed immediately,
                # so its outcome is left to the next pass.
                outcome.update(decision_class=cls, decision_code=code, execution_result=execution)
                await db.execute(update(ReplyJob).where(ReplyJob.id == job_id, ReplyJob.status == status)
                                 .values(outcome=outcome).execution_options(synchronize_session=False))
                await db.commit()
            owner_id, comment_id, video_id, draft_id = (job.channel_owner_id, job.comment_id,
                                                        job.video_id, job.draft_id)
            origin = "discovery" if (job.batch_id or "").startswith("discovery:") else "batch"
            trace = trace_for(draft, extra={"origin": origin, "job_status": status,
                                            "attempts": job.attempts,
                                            "reason": outcome.get("reason")})
    except Exception:
        logger.exception("event=decision_record_failed job=%s", job_id)
        return
    await record(owner_id=owner_id, comment_id=comment_id, video_id=video_id, draft_id=draft_id,
                 job_id=job_id, decision_class=cls, decision_code=code, execution_result=execution,
                 trace=trace)


async def latest_for_drafts(db, owner_id: str, draft_ids) -> dict:
    rows = await db.scalars(select(AutomationDecision).where(
        AutomationDecision.channel_owner_id == owner_id,
        AutomationDecision.draft_id.in_(list(draft_ids))).order_by(AutomationDecision.created_at))
    return {row.draft_id: row for row in rows}
