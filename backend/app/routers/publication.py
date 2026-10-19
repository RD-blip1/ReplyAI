"""Publishing one eligible draft as a public reply on YouTube.

This is the first and only path in the application that changes anything on YouTube, and
it is deliberately narrow. Manual publication requires one person to confirm the
connected channel, target, revision, and exact text. Automatic publication is called
only by a just-completed explicit generation/regeneration request when the channel's
current policy and every server-side safety check allow it. There is no schedule,
background worker, or batch operation.

The order of operations is the safety property. Everything is checked and the credential
is refreshed *before* the attempt is claimed, so a failure there posts nothing and
leaves no claim behind. The attempt is then committed, the insert is sent, and the
result is written. The commit happening before the send is what makes a lost response
recoverable: the record that a request may be public exists on disk before YouTube is
asked to make it public.
"""
import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    AppSession,
    DraftEvaluation,
    PublishedReplyGuard,
    PublishingSettings,
    ReplyDraft,
    ReplyPublication,
)
from app.models.session import utc_now
from app.routers.drafts import Csrf, Db, Revision, Session, fail, owned
from app.routers.youtube import _credentials, _owned_connection
from app.services import job_queue, publishing
from app.services.google_oauth import YOUTUBE_FORCE_SSL_SCOPE
from app.services.youtube_api import YouTubePublishFailure, insert_comment_reply


class _SlotRefused(Exception):
    """Internal marker: the automatic-publish budgets refused a reservation."""


class _DraftRef:
    """The fields record_automatic_failure needs, readable after a rollback."""

    def __init__(self, id, channel_owner_id, channel_id, video_id, comment_id, revision):
        self.id = id
        self.channel_owner_id = channel_owner_id
        self.channel_id = channel_id
        self.video_id = video_id
        self.comment_id = comment_id
        self.revision = revision


router = APIRouter(prefix="/api/drafts", tags=["reply-publications"])
logger = logging.getLogger(__name__)

SCOPE_MESSAGE = (
    "Google's youtube.force-ssl permission is required to post a reply. Use Update "
    "YouTube permissions first. Nothing has been posted."
)
# The provider's outcome vocabulary mapped onto the durable one. "rejected" is the only
# value that means nothing was posted, and it is recorded as "failed".
PUBLISH_STATUS = {"rejected": "failed", "unknown": "unknown"}


class Publish(Revision):
    """A confirmation of one specific saved reply.

    ``text`` is checked against the saved draft rather than used, so the request proves
    the person confirmed the text that is stored and cannot publish anything else.
    """

    text: str = Field(min_length=1, max_length=5000, pattern=r"\S")


async def eligibility(draft: ReplyDraft, connection, db) -> dict:
    """Whether this draft could be published right now, and why not if it cannot.

    Reported to the browser so the confirm action is not offered for a draft the server
    would refuse anyway. The server repeats every one of these checks on the request; this
    is a convenience, not the decision.
    """
    scope_available = YOUTUBE_FORCE_SSL_SCOPE in (connection.scopes or '').split()
    blocked = None if scope_available else ("insufficient_scope", SCOPE_MESSAGE)
    if blocked is None:
        guard = await publishing.guard_for(db, connection.channel_id, draft.comment_id)
        # A guard this draft owns is its own attempt, reported by the attempt itself.
        if guard is not None and guard.draft_id != draft.id:
            blocked = publishing.guard_message(guard)
    if blocked is None:
        blocked = publishing.blocking_reason(draft)
    return {
        "eligible": blocked is None,
        "blocked_code": blocked[0] if blocked else None,
        "blocked_message": blocked[1] if blocked else None,
        "scope_available": scope_available,
        "channel_title": connection.channel_title,
        "revision": draft.revision,
        "review": publishing.review_summary(draft),
    }


@router.get("/{draft_id}/publication")
async def read_publication(draft_id: str, session: Session, db: Db):
    """The posting state of this draft, and whether it may be published.

    Reads the database only: opening a draft, or opening the confirmation dialog, costs
    no provider request and writes nothing. This is what makes a reload show the same
    Posted, Unknown, or Failed state the person last saw.
    """
    draft = await owned(db, session, draft_id)
    connection = await _owned_connection(db, session)

    return {
        "publication": publishing.view(await publishing.latest_attempt(db, draft_id)),
        "eligibility": await eligibility(draft, connection, db),
    }


@router.post("/{draft_id}/publication")
async def publish(draft_id: str, body: Publish, session: Csrf, db: Db):
    """Post the saved reply to YouTube once, and record what happened.

    The request carries the revision and the confirmed text so the server can prove the
    confirmation matched the saved draft. It cannot supply the reply, the comment, the
    video, the channel, the approval, or the review; all of those come from the database.
    """
    draft = await owned(db, session, draft_id)
    connection = await _owned_connection(db, session)

    if body.revision != draft.revision:
        fail(409, "stale_draft", "The saved draft changed. Reload it before publishing.")
    if body.text != (draft.text or ""):
        # The confirmation was for different text. Refusing is the point of sending it.
        fail(409, "reply_text_changed",
             "The saved reply is not the text you confirmed. Reload it before publishing.")
    return await publish_saved_draft(db, draft, connection)


async def publish_generated_draft(
    draft: ReplyDraft,
    session: AppSession | None,
    db: AsyncSession,
    *,
    expected_mode: str,
    expected_revision: int,
    connection=None,
) -> dict | None:
    """Consider only the draft just generated by this request for automatic publishing.

    ``session`` is the browser session for the synchronous request path; a background
    worker passes ``connection=None``'s counterpart — an already-resolved
    ``YouTubeConnection`` — because it has no browser session. Exactly one of the two
    must identify the channel.

    Returns a decision dict (never raises for policy outcomes) so callers — including
    the batch worker — can record why automation did or did not act.
    """
    setting = await db.get(PublishingSettings, draft.channel_owner_id)
    mode = setting.mode if setting else "manual_review"
    revision = setting.revision if setting else 0
    if (mode != expected_mode or revision != expected_revision
            or mode == "manual_review" or draft.human_decision != "pending"):
        return {"mode": mode, "decision": "policy_changed", "published": None}

    blocked = publishing.automation_blocking_reason(draft, mode)

    if mode == "dry_run":
        if blocked is None:
            outcome = "would_publish"
        else:
            b_code = blocked[0]
            if b_code == "off_topic": outcome = "skipped_off_topic"
            elif b_code == "spam": outcome = "skipped_spam"
            elif b_code == "harassment": outcome = "skipped_harassment"
            elif b_code == "unsupported_language": outcome = "skipped_unsupported_language"
            elif b_code == "requires_human": outcome = "skipped_requires_human"
            elif b_code in ("context_required", "review_required", "models_disagree"):
                outcome = "needs_human_review"
            elif b_code in ("quality_not_passed", "confidence_insufficient", "review_findings"): outcome = "blocked_quality"
            elif b_code in ("triage_required", "generation_required", "manual_review_required"): outcome = "blocked_policy"
            else: outcome = "blocked_unexpected_reason"

        if outcome == "would_publish":
            try:
                if connection is None:
                    connection = await _owned_connection(db, session)
                else:
                    await db.refresh(connection)
                if connection.channel_id != draft.channel_id or YOUTUBE_FORCE_SSL_SCOPE not in (connection.scopes or '').split():
                    outcome = "missing_auth"
                else:
                    try:
                        await _credentials(connection, db)
                    except HTTPException as exc:
                        if exc.status_code == 401:
                            outcome = exc.detail.get("code", "reconnect_required") if isinstance(exc.detail, dict) else "reconnect_required"
                        else:
                            outcome = "unexpected_error"
                    except Exception:  # noqa: BLE001 - dry run must fail closed on any error
                        outcome = "unexpected_error"

                    if outcome == "would_publish":
                        guard = await publishing.guard_for(db, connection.channel_id, draft.comment_id)
                        if guard is not None and guard.draft_id != draft.id:
                            outcome = "unexpected_error"
                        attempt = await publishing.open_attempt(db, draft.id)
                        if attempt is not None:
                            outcome = "unexpected_error"
            except HTTPException as exc:
                if exc.status_code == 404:
                    outcome = "youtube_not_connected"
                else:
                    outcome = "unexpected_error"
            except Exception:  # noqa: BLE001 - dry run must fail closed on any error
                outcome = "unexpected_error"

        await record_automatic_failure(db, draft, expected_revision, outcome, blocked[1] if blocked else "Dry run evaluation complete.")
        return {"mode": mode, "decision": outcome,
                "reason": blocked[1] if blocked else "Dry run evaluation complete.",
                "published": None}

    if blocked:
        return {"mode": mode, "decision": blocked[0], "reason": blocked[1], "published": None}

    # Snapshot before publishing: a failure inside publish_saved_draft may roll the
    # transaction back, which expires the ORM object, and reading an expired attribute
    # in async context raises MissingGreenlet instead of reporting the real decision.
    draft_id, draft_revision, draft_text = draft.id, draft.revision, draft.text
    draft_owner_id = draft.channel_owner_id
    draft_channel_id = draft.channel_id
    draft_video_id = draft.video_id
    draft_comment_id = draft.comment_id
    # A plain stand-in with the same fields, so recording a failure after a rollback
    # never reads an expired ORM attribute.
    draft_ref = _DraftRef(draft_id, draft_owner_id, draft_channel_id,
                          draft_video_id, draft_comment_id, draft_revision)

    try:
        if connection is None:
            connection = await _owned_connection(db, session)

        result = await publish_saved_draft(
            db, draft, connection, automated_mode=mode,
            expected_settings_revision=expected_revision,
        )
        return {"mode": mode, "decision": "published", "published": result}
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code", "automatic_publish_failed")
        message = detail.get("message", "Automatic publishing could not be completed.")
        if code == "automatic_mode_changed":
            return {"mode": mode, "decision": code, "reason": message, "published": None}
        attempt = await publishing.latest_attempt(db, draft_id)
        if (attempt is not None and attempt.draft_revision == draft_revision
                and attempt.text == draft_text
                and attempt.status in ("posted", "failed", "unknown", "sending")):
            # The durable attempt already answers for this revision; nothing to record.
            return {"mode": mode, "decision": "attempt_recorded",
                    "attempt_status": attempt.status, "published": None}
        if code in {
            "auto_publish_rate_limited",
            "insufficient_scope",
            "reconnect_required",
            "oauth_setup_required",
            "youtube_not_connected",
            "connection_changed",
        }:
            await record_automatic_failure(db, draft_ref, expected_revision, code, message)
            return {"mode": mode, "decision": code, "reason": message, "published": None}
        # Conflicts are safe fallbacks: a human can inspect the existing attempt or
        # the draft. The existing durable publication state remains authoritative.
        if exc.status_code in (404, 409):
            return {"mode": mode, "decision": code, "reason": message, "published": None}
        raise


async def publish_saved_draft(
    db: AsyncSession,
    draft: ReplyDraft,
    connection,
    *,
    automated_mode: str | None = None,
    expected_settings_revision: int | None = None,
) -> dict:
    """Apply shared server-side checks and publish one frozen saved revision."""
    if connection.channel_id != draft.channel_id:
        fail(409, "connection_changed",
             "The channel connection changed. Reload the workspace.")
    if YOUTUBE_FORCE_SSL_SCOPE not in (connection.scopes or '').split():
        fail(403, "insufficient_scope", SCOPE_MESSAGE)

    if automated_mode is None:
        blocked = publishing.blocking_reason(draft)
    else:
        setting = await db.get(PublishingSettings, draft.channel_owner_id)
        if (setting is None or setting.mode != automated_mode
                or setting.revision != expected_settings_revision):
            fail(409, "automatic_mode_changed", "Automatic publishing was turned off.")
        blocked = publishing.automation_blocking_reason(draft, automated_mode)
        if draft.human_decision != "pending":
            blocked = ("human_decision_changed", "This reply now requires human review.")
    if blocked:
        fail(409, blocked[0], blocked[1])

    # The durable half of the duplicate boundary is checked for a useful error and
    # enforced by the unique index inside claim().
    guard = await publishing.guard_for(db, connection.channel_id, draft.comment_id)
    if guard is not None and guard.draft_id != draft.id:
        code, message = publishing.guard_message(guard)
        fail(409, code, message)

    attempt = await publishing.open_attempt(db, draft.id)
    if attempt is not None:
        confirmed = (attempt.draft_revision == draft.revision
                     and attempt.text == (draft.text or ""))
        if attempt.status == "posted":
            if confirmed:
                # Confirming twice, or a retried request whose response was lost, returns
                # the recorded result. Nothing is inserted a second time.
                return publishing.view(attempt)
            # The draft was edited after its reply went public. The old confirmation is
            # not a confirmation of the new text, and the published reply cannot be
            # un-posted by publishing again.
            fail(409, "already_published",
                 "This draft's reply was already posted on YouTube. It cannot be "
                 "published again, including after further edits.")
        fail(409, "publication_unresolved",
             "An earlier attempt to post this reply has an unresolved outcome. Check the "
             "video's replies on YouTube before trying again, so the reply is not posted twice.")

    # Refresh before claiming so an unavailable credential never leaves a publication
    # reservation behind for a request that was not sent.
    credentials = await _credentials(connection, db)

    await db.refresh(connection)
    if connection.channel_id != draft.channel_id:
        fail(409, "connection_changed",
             "The channel connection changed. Reload the workspace.")
    if YOUTUBE_FORCE_SSL_SCOPE not in (connection.scopes or '').split():
        fail(403, "insufficient_scope", SCOPE_MESSAGE)

    publication = await claim(
        db,
        draft,
        automated_mode=automated_mode,
        expected_settings_revision=expected_settings_revision,
    )
    if automated_mode is not None:
        setting = await db.get(PublishingSettings, draft.channel_owner_id)
        if setting is not None:
            await db.refresh(setting)
        await db.refresh(connection)
        if (setting is None or setting.mode != automated_mode
                or setting.revision != expected_settings_revision):
            await settle(
                db, publication, "failed",
                failure={"code": "automatic_mode_changed",
                         "message": "Automatic publishing was turned off before sending."},
            )
            fail(409, "automatic_mode_changed", "Automatic publishing was turned off.")
        if (connection.channel_id != draft.channel_id
                or YOUTUBE_FORCE_SSL_SCOPE not in (connection.scopes or '').split()):
            await settle(
                db, publication, "failed",
                failure={"code": "insufficient_scope", "message": SCOPE_MESSAGE},
            )
            fail(403, "insufficient_scope", SCOPE_MESSAGE)

    try:
        reply_id = await insert_comment_reply(
            credentials, draft.comment_id, draft.text
        )
    except YouTubePublishFailure as exc:
        # Snapshot before the rollback below expires the ORM objects: reading an
        # expired attribute would lazily reload it outside a greenlet and raise
        # MissingGreenlet, replacing a correct decision with an unexpected error.
        draft_id, operation_id = draft.id, publication.operation_id
        try:
            await settle(
                db, publication, PUBLISH_STATUS[exc.outcome],
                failure={"code": exc.code, "message": exc.message},
            )
        except SQLAlchemyError:
            # The provider already refused the insert, so nothing was posted regardless
            # of what the database managed to keep. The attempt stays unresolved on disk,
            # which is conservative: it blocks a retry until a person checks YouTube.
            await db.rollback()
            logger.warning(
                "Could not record publication outcome draft=%s operation=%s code=%s",
                draft_id, operation_id, exc.code,
            )
        fail(exc.status_code, exc.code, exc.message)

    try:
        await settle(db, publication, "posted", reply_comment_id=reply_id)
    except SQLAlchemyError:
        # The reply is public but the confirmation could not be stored. Reporting it as
        # posted would be a claim the database cannot support, and reporting it as
        # failed would invite a duplicate, so it is reported as unknown.
        draft_id, operation_id = draft.id, publication.operation_id
        await db.rollback()
        logger.warning(
            "Reply was created but its record could not be saved draft=%s operation=%s",
            draft_id, operation_id,
        )
        fail(502, "publish_outcome_unknown",
             "The reply may have been posted, but ReplyAI could not record it. Check the "
             "video's replies on YouTube before trying again.")
    return publishing.view(publication)



async def record_automatic_failure(
    db: AsyncSession, draft: ReplyDraft, policy_revision: int, code: str, message: str
) -> None:
    """Persist an automatic failure or dry run evaluation without side effects."""
    now = utc_now()
    db.add(DraftEvaluation(
        channel_owner_id=draft.channel_owner_id,
        channel_id=draft.channel_id,
        video_id=draft.video_id,
        comment_id=draft.comment_id,
        draft_id=draft.id,
        draft_revision=draft.revision,
        policy_revision=policy_revision,
        decision=code,
        reason=message,
        created_at=now,
    ))
    await db.commit()


async def claim(
    db: AsyncSession,
    draft: ReplyDraft,
    *,
    automated_mode: str | None = None,
    expected_settings_revision: int | None = None,
) -> ReplyPublication:
    """Record the attempt, reserve the comment, and take the draft, in one transaction.

    Three schema rules are the authority here, not convention. The unique operation id
    means one attempt has one token; the partial unique index means a draft can hold only
    one open attempt; and ``uq_published_reply_guard`` means only one ReplyAI reply can
    ever be reserved against a given channel and parent comment, which is the rule that
    still holds after the draft is deleted.

    Reserving before the send is what closes the concurrent gap. Two requests for the
    same comment cannot both pass a "has anyone replied here" check, because neither can
    see the other's uncommitted work. They race on that index instead, and one loses.

    The draft's ``operation_token`` is set to the same value, which is what makes every
    existing mutation, and any later one, refuse to touch a draft being published.
    """
    token = str(uuid.uuid4())
    publication = ReplyPublication(
        draft_id=draft.id, operation_id=token, channel_id=draft.channel_id,
        video_id=draft.video_id, parent_comment_id=draft.comment_id,
        draft_revision=draft.revision, text=draft.text, status="sending",
    )
    try:
        if automated_mode is not None:
            await reserve_automatic_slot(
                db, draft.channel_owner_id, automated_mode, expected_settings_revision
            )
        db.add(publication)
        # Flush first so the attempt has its id. The guard records which attempt owns the
        # reservation, and settle() uses that to release only its own row.
        await db.flush()
        db.add(PublishedReplyGuard(
            channel_id=draft.channel_id, parent_comment_id=draft.comment_id,
            status="sending", draft_id=draft.id, publication_id=publication.id,
        ))
        await db.flush()
        result = await db.execute(
            update(ReplyDraft)
            .where(
                ReplyDraft.id == draft.id,
                ReplyDraft.revision == draft.revision,
                ReplyDraft.operation_token.is_(None),
                ReplyDraft.publication_token.is_(None),
                ReplyDraft.human_decision == (
                    "approved" if automated_mode is None else "pending"
                ),
            )
            .values(operation_token=token, publication_token=token, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            fail(409, "draft_conflict", "Draft changed or is busy. Reload and retry.")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Which of the three constraints refused is not reported. The only two a person
        # can act on are covered by the checks before this point; everything else means
        # the same thing to them: something already holds this attempt.
        fail(409, "publication_conflict", "This draft is already being published.")
    return publication


async def reserve_automatic_slot(
    db: AsyncSession, owner_id: str, mode: str, expected_revision: int | None
) -> None:
    """Atomically recheck mode and reserve one automatic send against both budgets.

    The hourly window stays on ``publishing_settings`` (existing behavior). The daily
    budget is a shared ``rate_limit_counters`` token acquired inside this same
    transaction, so the token commits or rolls back with the claim and two workers
    cannot both take the last one.

    Both reservations happen inside one savepoint. A refusal rolls the savepoint back,
    which discards a daily token staged just before an hourly refusal (and an hourly
    increment staged just before a daily refusal). A session-wide rollback would do the
    same but also expire the caller's ORM objects, which an AsyncSession cannot reload
    outside a greenlet.
    """
    now = utc_now()
    expires_before = now - timedelta(hours=1)
    reset_window = or_(
        PublishingSettings.auto_publish_window_started_at.is_(None),
        PublishingSettings.auto_publish_window_started_at <= expires_before,
    )
    refusal: str | None = None
    try:
        async with db.begin_nested():
            if not await job_queue.try_acquire(
                db,
                f"auto_publish:day:{owner_id}",
                per_minute=0,
                per_day=settings.auto_publish_max_per_day,
                commit=False,
            ):
                refusal = "day"
                raise _SlotRefused
            result = await db.execute(
                update(PublishingSettings)
                .where(
                    PublishingSettings.channel_owner_id == owner_id,
                    PublishingSettings.mode == mode,
                    PublishingSettings.revision == expected_revision,
                    or_(
                        reset_window,
                        PublishingSettings.auto_publish_count
                        < settings.auto_publish_max_per_hour,
                    ),
                )
                .values(
                    auto_publish_count=case(
                        (reset_window, 1),
                        else_=PublishingSettings.auto_publish_count + 1,
                    ),
                    auto_publish_window_started_at=case(
                        (reset_window, now),
                        else_=PublishingSettings.auto_publish_window_started_at,
                    ),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 1:
                return
            refusal = "hour"
            raise _SlotRefused
    except _SlotRefused:
        pass

    setting = await db.get(PublishingSettings, owner_id, populate_existing=True)
    if setting is None or setting.mode != mode:
        fail(409, "automatic_mode_changed", "Automatic publishing was turned off.")
    if refusal == "day":
        fail(
            429,
            "auto_publish_rate_limited",
            f"Automatic publishing is limited to {settings.auto_publish_max_per_day} "
            "replies per day.",
        )
    fail(
        429,
        "auto_publish_rate_limited",
        f"Automatic publishing is limited to {settings.auto_publish_max_per_hour} replies per hour.",
    )


async def settle(
    db: AsyncSession,
    publication: ReplyPublication,
    status: str,
    *,
    reply_comment_id: str | None = None,
    failure: dict | None = None,
) -> None:
    """Record what the attempt did, resolve its guard, and release the draft.

    The guard is resolved in the same transaction as the attempt, so the two can never
    disagree: a confirmed reply is recorded on both together, and a definitive refusal
    is recorded as a failure with its reservation deleted, which is what leaves a
    deliberate retry free to reserve the comment again.

    The draft is released by clearing only the token this attempt owns, so an attempt
    that has finished can never leave a draft busy, and a second attempt can never
    release it early.
    """
    publication.status = status
    publication.reply_comment_id = reply_comment_id
    publication.failure = failure or {}
    publication.resolved_at = utc_now()
    guard = await publishing.guard_for(db, publication.channel_id,
                                       publication.parent_comment_id)
    if guard is not None and guard.publication_id == publication.id:
        await publishing.release_guard(
            db, guard.id, reply_comment_id=reply_comment_id, status=status)
    await db.execute(
        update(ReplyDraft)
        .where(
            ReplyDraft.id == publication.draft_id,
            ReplyDraft.operation_token == publication.operation_id,
            ReplyDraft.publication_token == publication.operation_id,
        )
        .values(operation_token=None, publication_token=None, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
