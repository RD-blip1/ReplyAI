"""Local drafts: drafting, review, refinement, and human decisions.

Nothing here posts to YouTube. The single publishing route lives in
``app.routers.publication``, which reads this module's ownership and draft-state rules
so that a reply can only be published from a draft these rules already protect.
"""

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_current_session, get_db, require_csrf
from app.models import (
    AppSession,
    ChannelOwner,
    DraftUsage,
    PublishingSettings,
    RefinementTurn,
    ReplyDraft,
    ReplyPreferences,
)
from app.models.session import utc_now
from app.routers.youtube import _credentials, _owned_channel, _owned_connection
from app.services import (
    draft_workflow,
    llm,
    publishing,
    refinement,
    reply_preferences,
    video_context,
)
from app.services import usage as usage_service
from app.services.google_oauth import has_comment_read_scope
from app.services.youtube_api import YouTubeFailure, get_owned_comment

router = APIRouter(prefix="/api/drafts", tags=["local-drafts"])
logger = logging.getLogger(__name__)
Db = Annotated[AsyncSession, Depends(get_db)]
Session = Annotated[AppSession, Depends(get_current_session)]
Csrf = Annotated[AppSession, Depends(require_csrf)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Create(Input):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    comment_id: str = Field(min_length=1, max_length=256)


class Revision(Input):
    revision: int = Field(ge=1, strict=True)


class Edit(Revision):
    text: str = Field(min_length=1, max_length=5000, pattern=r"\S")


class Decision(Revision):
    decision: Literal["approved", "rejected"]


class Refine(Revision):
    instruction: str = Field(
        min_length=1, max_length=refinement.MAX_INSTRUCTION_CHARS, pattern=r"\S"
    )


def fail(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def configured():
    if not llm.setup_status()["configured"]:
        fail(503, "ai_setup_required", "AI setup required. Configure both models on the backend.")


def view(draft: ReplyDraft):
    return {
        column.name: getattr(draft, column.name)
        for column in ReplyDraft.__table__.columns
        if column.name not in ("channel_owner_id", "operation_token", "publication_token")
    }


async def owned(db: AsyncSession, session: AppSession, draft_id: str):
    """Fetch a draft the caller may act on, by way of their verified channel.

    Ownership is resolved from the OAuth-verified connection, so this succeeds after a
    reconnect to the same channel and fails for a different channel, for a caller with
    no connection, and while the channel is disconnected. A draft id on its own is never
    enough to reach a record.
    """
    owner = await _owned_channel(db, session)
    draft = await db.scalar(
        select(ReplyDraft).where(ReplyDraft.id == draft_id, ReplyDraft.channel_owner_id == owner.id)
    )
    if draft is None:
        fail(404, "draft_unavailable", "Draft not found for this channel.")
    return draft


async def snapshot(db, session, video_id, comment_id):
    connection = await _owned_connection(db, session)
    if not has_comment_read_scope(connection.scopes):
        fail(
            403,
            "insufficient_scope",
            "Update YouTube permissions to restore comment access. The youtube.force-ssl "
            "permission is required; your existing connection will be preserved.",
        )
    credentials = await _credentials(connection, db)
    try:
        text = await get_owned_comment(connection, credentials, video_id, comment_id)
    except YouTubeFailure as exc:
        fail(exc.status_code, exc.code, exc.message)
    return connection, text


async def change(db, draft, revision, values, *, idle=False, claimed=False):
    predicates = [ReplyDraft.id == draft.id, ReplyDraft.revision == revision]
    if idle:
        predicates.append(
            or_(
                ReplyDraft.operation_token.is_(None),
                ReplyDraft.updated_at < utc_now() - timedelta(minutes=6),
            )
        )
    if claimed:
        # A publication attempt owns this draft. An in-flight review may be superseded
        # by an edit because its late result is simply thrown away, but a publish may
        # already be public, so the text it was sent must not change underneath it.
        predicates.append(ReplyDraft.publication_token.is_(None))
    result = await db.execute(
        update(ReplyDraft)
        .where(*predicates)
        .values(**values, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        fail(409, "draft_conflict", "Draft changed or is busy. Reload its saved state and retry.")
    await db.commit()
    await db.refresh(draft)


def identifiers():
    return {
        role: {"base_url": llm.configuration(role)[0], "model": llm.configuration(role)[2]}
        for role in ("primary", "secondary")
    }


StageResult = draft_workflow.StageResult


async def call(role, schema, instruction, data) -> StageResult:
    return await draft_workflow.call_stage(role, schema, instruction, data)


def langgraph_enabled() -> bool:
    return settings.draft_workflow_engine == "langgraph"


async def run_models(
    db,
    draft,
    *,
    generate,
    operation,
    preferences=None,
    channel_title="",
    video_title="",
    video_description="",
    publishing_mode="manual_review",
    publishing_revision=0,
    publish_handoff=None,
):
    engine = settings.draft_workflow_engine
    if operation in ("generate", "regenerate"):
        logger.info(
            "event=draft_workflow_engine_selected engine=%s operation=%s",
            engine,
            operation,
        )
    if engine == "langgraph":
        from app.services import draft_graph

        try:
            saved = await draft_graph.run_draft_graph(
                db,
                draft,
                generate=generate,
                operation=operation,
                preferences=preferences,
                channel_title=channel_title,
                video_title=video_title,
                video_description=video_description,
                publishing_mode=publishing_mode,
                publishing_revision=publishing_revision,
                handoff=publish_handoff,
            )
        except draft_workflow.StaleWorkflowResult:
            draft_workflow.raise_stale_workflow()
        return view(saved)

    return await run_models_legacy(
        db,
        draft,
        generate=generate,
        operation=operation,
        preferences=preferences,
        channel_title=channel_title,
    )


async def run_models_legacy(db, draft, *, generate, operation, preferences=None, channel_title="", **kwargs):
    revision, token = draft.revision, draft.operation_token
    original = {"comment": draft.original_comment}
    failures = {}
    usage = dict(draft.token_usage or {})
    spent = []
    primary, secondary = draft.primary_classification, draft.secondary_classification
    text = draft.text

    def remember(stage, result):
        """Collect one entry per HTTP request the stage actually issued.

        Only real calls reach here. A stage that reuses a stored result without
        contacting the provider is never remembered, so reuse is not billed twice.
        """
        spent.extend(
            draft_workflow.usage_attempts(
                result,
                draft_id=draft.id,
                revision=revision,
                operation_token=token,
                operation=operation,
                stage=stage,
            )
        )

    if generate:
        generation_data = {
            **original,
            "video_id": draft.video_id,
            "channel": {"channel_id": draft.channel_id, "title": channel_title},
            "response_preferences": preferences or reply_preferences.context(None),
        }
        generated, classified = await asyncio.gather(
            call(
                "primary",
                llm.Generation,
                reply_preferences.generation_instruction(),
                generation_data,
            ),
            call(
                "secondary",
                llm.Classification,
                "Independently classify the original comment sentiment and intent.",
                original,
            ),
        )
        primary = generated.value["classification"] if generated.value else None
        text = generated.value["reply"] if generated.value else ""
        secondary = classified.value
        usage["generation"] = generated.usage
        usage["classification"] = classified.usage
        remember("generation", generated)
        remember("classification", classified)
        for stage, stage_error in (
            ("generation", generated.error),
            ("classification", classified.error),
        ):
            if stage_error:
                failures[stage] = stage_error
                usage.pop(stage, None)
    elif secondary is None:
        classified = await call(
            "secondary",
            llm.Classification,
            "Independently classify the original comment.",
            original,
        )
        secondary = classified.value
        usage["classification"] = classified.usage
        remember("classification", classified)
        if classified.error:
            failures["classification"] = classified.error
            usage.pop("classification", None)
    quality = None
    if text:
        reviewed = await call(
            "secondary",
            llm.Quality,
            "Assess the proposed reply for relevance, unsupported claims, and inappropriate "
            "content. pass means no issue identified; concern means an issue; uncertain means "
            "insufficient evidence. Give concise findings and a confidence score from 0 to 1 "
            "for this review. This is not human approval.",
            {**original, "reply": text},
        )
        quality = reviewed.value
        usage["quality"] = reviewed.usage
        remember("quality", reviewed)
        if reviewed.error:
            failures["quality"] = reviewed.error
            usage.pop("quality", None)
    else:
        failures["quality"] = "generation_required"
    try:
        saved = await draft_workflow.persist_result(
            db,
            draft,
            revision=revision,
            operation_token=token,
            text=text,
            primary=primary,
            secondary=secondary,
            quality=quality,
            failures=failures,
            usage=usage,
            spent=spent,
            triage=None,
        )
    except draft_workflow.StaleWorkflowResult:
        draft_workflow.raise_stale_workflow()
    return view(saved)


def retained_usage(draft, *stages):
    """Keep usage only for stages whose stored result survives this change."""
    current = draft.token_usage or {}
    return {stage: current[stage] for stage in stages if current.get(stage)}


@router.get("/setup")
async def setup(session: Session):
    return llm.setup_status()


@router.get("")
async def find_draft(
    session: Session,
    db: Db,
    video_id: Annotated[str, Query(max_length=64)],
    comment_id: Annotated[str, Query(max_length=256)],
):
    owner = await _owned_channel(db, session)
    draft = await db.scalar(
        select(ReplyDraft).where(
            ReplyDraft.channel_owner_id == owner.id,
            ReplyDraft.video_id == video_id,
            ReplyDraft.comment_id == comment_id,
        )
    )
    return view(draft) if draft else None


@router.get("/{draft_id}")
async def retrieve(draft_id: str, session: Session, db: Db):
    return view(await owned(db, session, draft_id))


@router.get("/{draft_id}/usage")
async def usage_history(draft_id: str, session: Session, db: Db):
    """Every recorded provider request for this draft, plus totals and coverage.

    ``token_usage`` on the draft is the per-stage snapshot of the most recent call;
    this ledger is the record of actual consumption. Rows cover discarded operations
    too, because those requests were still made. Requests with no reported counts stay
    null and are never estimated, and a draft with activity from before the ledger
    began is reported as incomplete rather than as a lifetime total.
    """
    draft = await owned(db, session, draft_id)
    rows = (
        (
            await db.execute(
                select(DraftUsage)
                .where(DraftUsage.draft_id == draft_id)
                .order_by(DraftUsage.created_at, DraftUsage.id)
            )
        )
        .scalars()
        .all()
    )
    entries = [
        {
            "revision": row.revision,
            "operation": row.operation,
            "stage": row.stage,
            "role": row.role,
            "attempt": row.attempt,
            "model": row.model,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "consumption": row.consumption,
            "applied": row.applied,
        }
        for row in rows
    ]
    unknown = [row for row in entries if row["consumption"] == "unknown"]
    # Two independent ways a draft's history can be incomplete. Either one means earlier
    # usage is unavailable, so the totals are never presented as lifetime totals.
    before_tracking = (
        usage_service.as_utc(draft.created_at) < usage_service.USAGE_TRACKING_STARTED_AT
    )
    # A stage snapshot that no recorded request accounts for means requests happened
    # without being ledgered, for example while the running worker predated the ledger.
    recorded = {(row.stage, row.model, row.input_tokens, row.output_tokens) for row in rows}
    unbacked = sorted(
        stage
        for stage, value in (draft.token_usage or {}).items()
        if (
            stage,
            (value or {}).get("model", ""),
            (value or {}).get("input_tokens"),
            (value or {}).get("output_tokens"),
        )
        not in recorded
    )
    incomplete = before_tracking or bool(unbacked)
    return {
        "entries": entries,
        "totals": {
            "requests": len(entries),
            "reported_requests": len(entries) - len(unknown),
            "unknown_requests": len(unknown),
            "discarded_requests": sum(1 for row in entries if not row["applied"]),
            "input_tokens": sum(row["input_tokens"] or 0 for row in entries),
            "output_tokens": sum(row["output_tokens"] or 0 for row in entries),
        },
        "coverage": {
            "complete": not incomplete,
            "pre_ledger_activity": incomplete,
            "unbacked_snapshot_stages": unbacked,
            "notice": usage_service.PRE_LEDGER_NOTICE if incomplete else None,
            "unknown_notice": usage_service.UNKNOWN_CONSUMPTION_NOTICE if unknown else None,
        },
    }


@router.post("")
async def create(body: Create, session: Csrf, db: Db):
    configured()
    connection, text = await snapshot(db, session, body.video_id, body.comment_id)
    owner = await _owned_channel(db, session)
    saved_preferences = await db.get(ReplyPreferences, owner.id)
    saved_publishing = await db.get(PublishingSettings, owner.id)
    generation_mode = saved_publishing.mode if saved_publishing else "manual_review"
    generation_mode_revision = saved_publishing.revision if saved_publishing else 0


    # Fetch video context
    credentials = await _credentials(connection, db)
    vid_ctx = await video_context.get_video_context(credentials, body.video_id)
    vid_title = vid_ctx.get("title") or ""
    vid_desc = vid_ctx.get("description") or ""
    draft = ReplyDraft(
        channel_owner_id=owner.id,
        channel_id=connection.channel_id,
        video_id=body.video_id,
        comment_id=body.comment_id,
        original_comment=text,
        providers=identifiers(),
        operation_token=str(uuid.uuid4()),
    )
    db.add(draft)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        fail(409, "draft_exists", "A saved draft exists. Reload it before regenerating.")
    result = await run_models(
        db,
        draft,
        generate=True,
        operation="generate",
        preferences=reply_preferences.context(saved_preferences),
        channel_title=connection.channel_title,
        video_title=vid_title,
        video_description=vid_desc,
        publishing_mode=generation_mode,
        publishing_revision=generation_mode_revision,
        publish_handoff=(
            lambda: consider_automatic_publication(
                db, session, draft, generation_mode, generation_mode_revision
            )
        )
        if langgraph_enabled()
        else None,
    )
    if not langgraph_enabled():
        await consider_automatic_publication(
            db, session, draft, generation_mode, generation_mode_revision
        )
    return result


@router.post("/{draft_id}/edit")
async def edit(draft_id: str, body: Edit, session: Csrf, db: Db):
    draft = await owned(db, session, draft_id)
    await change(
        db,
        draft,
        body.revision,
        {
            "text": body.text.strip(),
            "revision": body.revision + 1,
            "quality": None,
            "review_status": "pending",
            "human_decision": "pending",
            "failure": {},
            "operation_token": None,
            "token_usage": retained_usage(draft, "classification"),
        },
        claimed=True,
    )
    return view(draft)


@router.post("/{draft_id}/regenerate")
async def regenerate(draft_id: str, body: Revision, session: Csrf, db: Db):
    configured()
    draft = await owned(db, session, draft_id)
    connection, text = await snapshot(db, session, draft.video_id, draft.comment_id)
    if connection.channel_id != draft.channel_id:
        fail(409, "connection_changed", "The channel connection changed. Reload the workspace.")
    saved_preferences = await db.get(ReplyPreferences, draft.channel_owner_id)
    saved_publishing = await db.get(PublishingSettings, draft.channel_owner_id)
    generation_mode = saved_publishing.mode if saved_publishing else "manual_review"
    generation_mode_revision = saved_publishing.revision if saved_publishing else 0
    
    credentials = await _credentials(connection, db)
    vid_ctx = await video_context.get_video_context(credentials, draft.video_id)
    vid_title = vid_ctx.get("title") or ""
    vid_desc = vid_ctx.get("description") or ""

    await change(
        db,
        draft,
        body.revision,
        {
            "original_comment": text,
            "text": "",
            "revision": body.revision + 1,
            "providers": identifiers(),
            "primary_classification": None,
            "secondary_classification": None,
            "agreement": "pending",
            "quality": None,
            "generation_status": "pending",
            "review_status": "pending",
            "human_decision": "pending",
            "failure": {},
            "operation_token": str(uuid.uuid4()),
            "token_usage": {},
        },
        idle=True,
    )
    result = await run_models(
        db,
        draft,
        generate=True,
        operation="regenerate",
        preferences=reply_preferences.context(saved_preferences),
        channel_title=connection.channel_title,
        video_title=vid_title,
        video_description=vid_desc,
        publishing_mode=generation_mode,
        publishing_revision=generation_mode_revision,
        publish_handoff=(
            lambda: consider_automatic_publication(
                db, session, draft, generation_mode, generation_mode_revision
            )
        )
        if langgraph_enabled()
        else None,
    )
    if not langgraph_enabled():
        await consider_automatic_publication(
            db, session, draft, generation_mode, generation_mode_revision
        )
    return result


async def consider_automatic_publication(
    db, session, draft, generation_mode, generation_mode_revision
):
    # Imported at the call site because the publication router reuses draft ownership
    # dependencies from this module.
    from app.routers.publication import publish_generated_draft

    await publish_generated_draft(
        draft,
        session,
        db,
        expected_mode=generation_mode,
        expected_revision=generation_mode_revision,
    )


@router.post("/{draft_id}/review")
async def review(draft_id: str, body: Revision, session: Csrf, db: Db):
    configured()
    draft = await owned(db, session, draft_id)
    if not draft.text or not draft.primary_classification:
        fail(409, "generation_required", "Regenerate the draft before requesting a review.")
    providers = dict(draft.providers)
    providers["secondary"] = identifiers()["secondary"]
    secondary = draft.secondary_classification if providers == draft.providers else None
    await change(
        db,
        draft,
        body.revision,
        {
            "providers": providers,
            "secondary_classification": secondary,
            "agreement": llm.agreement(draft.primary_classification, secondary),
            "quality": None,
            "review_status": "pending",
            "human_decision": "pending",
            "failure": {},
            "operation_token": str(uuid.uuid4()),
            "token_usage": retained_usage(draft, "generation", "classification"),
        },
        idle=True,
    )
    return await run_models(db, draft, generate=False, operation="review")


@router.post("/{draft_id}/decision")
async def decision(draft_id: str, body: Decision, session: Csrf, db: Db):
    draft = await owned(db, session, draft_id)
    if body.decision == "approved" and (
        draft.review_status != "complete" or not draft.quality or not draft.text
    ):
        fail(409, "review_required", "Complete review of the saved revision before approval.")
    result = await db.execute(
        update(ReplyDraft)
        .where(
            ReplyDraft.id == draft.id,
            ReplyDraft.revision == body.revision,
            ReplyDraft.operation_token.is_(None),
            ReplyDraft.publication_token.is_(None),
            ReplyDraft.review_status == draft.review_status,
        )
        .values(human_decision=body.decision, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        fail(409, "draft_conflict", "Draft changed or is busy. Reload and retry.")
    await db.commit()
    await db.refresh(draft)
    return view(draft)


@router.delete("", status_code=204)
async def delete_retained(session: Csrf, db: Db) -> Response:
    """Permanently delete this channel's retained drafts and usage history.

    This is the only destructive path for retained work, and it is deliberately separate
    from disconnecting: disconnecting removes the credential and ends access while
    leaving the drafts in place, so reconnecting the same verified channel restores them.
    Deleting the channel owner cascades to its drafts and their usage rows, and a later
    reconnect starts from an empty history rather than resurrecting this one.

    A reply whose posting outcome is still unresolved blocks this. That request may
    already be public, and the record of it is the only thing saying so, so it cannot be
    cascaded away while a person still needs to check YouTube.
    """
    owner = await _owned_channel(db, session)
    if await publishing.unresolved_for_owner(db, owner.id) is not None:
        fail(
            409,
            "publication_unresolved",
            "A reply from this channel has an unresolved posting outcome. Check its "
            "replies on YouTube before deleting retained work.",
        )
    await db.execute(delete(ChannelOwner).where(ChannelOwner.id == owner.id))
    await db.commit()
    return Response(status_code=204)


async def _turn(db: AsyncSession, draft: ReplyDraft, turn_id: str) -> RefinementTurn:
    """Load a conversation turn that belongs to this draft.

    Scoping the lookup to the draft is what keeps one channel's turns unreadable through
    another channel's draft id, and keeps a turn id from being usable as a handle on a
    draft the caller does not own.
    """
    turn = await db.scalar(
        select(RefinementTurn).where(
            RefinementTurn.id == turn_id, RefinementTurn.draft_id == draft.id
        )
    )
    if turn is None:
        fail(404, "suggestion_unavailable", "That suggestion is not part of this draft.")
    return turn


@router.get("/{draft_id}/conversation")
async def conversation(draft_id: str, session: Session, db: Db):
    """The refinement conversation for this draft. Reading it costs no model call.

    Turns come from the database rather than the request, so a client cannot present a
    history that was never had, and a reload or a same-channel reconnection shows the
    same conversation.
    """
    draft = await owned(db, session, draft_id)
    turns = (
        (
            await db.execute(
                select(RefinementTurn)
                .where(RefinementTurn.draft_id == draft.id)
                .order_by(RefinementTurn.created_at, RefinementTurn.id)
            )
        )
        .scalars()
        .all()
    )
    conversation_turns = []
    for turn in turns:
        item = refinement.view(turn)
        if item["status"] == "suggested" and item["base_revision"] != draft.revision:
            item["status"] = "stale"
        conversation_turns.append(item)
    return {"turns": conversation_turns}


@router.post("/{draft_id}/refine")
async def refine(draft_id: str, body: Refine, session: Csrf, db: Db):
    """Produce a suggested rewrite for the saved reply. The saved draft is not touched.

    The proposal is stored on its own turn, so the saved reply, its revision, its human
    approval, and its quality review are all left exactly as they were. Nothing here
    posts to YouTube or claims to have; the model is given no tools.

    One instruction is one primary-model call, and every HTTP attempt it makes is
    recorded in the usage ledger whether or not a suggestion comes back.
    """
    configured()
    draft = await owned(db, session, draft_id)
    if not draft.text:
        fail(409, "generation_required", "Generate or save reply text before refining.")
    if body.revision != draft.revision:
        # The client is refining against a revision that is no longer saved. Refining
        # anyway would silently answer the wrong question, because the context would be
        # a reply the user is no longer looking at.
        fail(409, "stale_draft", "The saved draft changed. Reload it before refining.")

    turns = (
        (
            await db.execute(
                select(RefinementTurn)
                .where(RefinementTurn.draft_id == draft.id)
                .order_by(RefinementTurn.created_at, RefinementTurn.id)
            )
        )
        .scalars()
        .all()
    )
    instruction = body.instruction.strip()
    saved_preferences = await db.get(ReplyPreferences, draft.channel_owner_id)
    connection = await _owned_connection(db, session)
    context = refinement.build_context(draft, turns, instruction)
    context["channel"] = {
        "channel_id": connection.channel_id,
        "title": connection.channel_title,
    }
    context["response_preferences"] = reply_preferences.context(saved_preferences)
    result = await call("primary", llm.Refinement, refinement.REFINEMENT_INSTRUCTION, context)
    token = str(uuid.uuid4())
    spent = [
        usage_service.entry(
            draft_id=draft.id,
            revision=body.revision,
            operation_token=token,
            operation="refine",
            stage="refinement",
            role="primary",
            attempt=attempt,
            reported=reported,
            applied=False,
        )
        for attempt, reported in enumerate(result.attempts, start=1)
    ]

    if result.error or not result.value:
        # No suggestion, but the requests were still made and may have been billed, so
        # they are recorded before the failure is reported.
        await usage_service.record_attempts(spent)
        status, message = refinement.failure(result.error or "invalid_response")
        fail(status, result.error or "invalid_response", message)

    suggestion = result.value["reply"].strip()
    if len(suggestion) > refinement.MAX_SUGGESTION_CHARS:
        await usage_service.record_attempts(spent)
        fail(
            502,
            "invalid_response",
            "The suggested reply was too long to accept. Ask for something shorter.",
        )
    # The saved draft may have moved while the model was working. Re-read the revision
    # from the database rather than trusting the copy loaded before the call, otherwise
    # a late response would be labelled current and look applicable when it is not.
    current_revision = await db.scalar(select(ReplyDraft.revision).where(ReplyDraft.id == draft.id))
    stale = current_revision != body.revision
    turn = RefinementTurn(
        draft_id=draft.id,
        base_revision=body.revision,
        instruction=instruction,
        suggestion=suggestion,
        status="stale" if stale else "suggested",
        operation_token=token,
    )
    db.add(turn)
    await db.commit()
    # The turn's transaction is settled, so the accounting session commits independently.
    for row in spent:
        row["applied"] = not stale
    await usage_service.record_attempts(spent)
    await db.refresh(draft)
    return {"turn": refinement.view(turn), "draft": view(draft)}


@router.post("/{draft_id}/refine/{turn_id}/apply")
async def apply_suggestion(draft_id: str, turn_id: str, body: Revision, session: Csrf, db: Db):
    """Save a suggested rewrite as a new revision of the draft.

    The saved revision, the turn's base revision, and the draft's busy state are all
    checked in the same statement that writes the change, so a suggestion can never land
    on top of newer work. Approval and the quality review are cleared because they
    described the previous text. No model call happens here: the rewritten reply is
    unreviewed until the existing "Review edited draft" action is used.
    """
    draft = await owned(db, session, draft_id)
    turn = await _turn(db, draft, turn_id)
    if turn.status == "discarded":
        fail(409, "suggestion_discarded", "That suggestion was discarded.")
    if turn.status == "applied":
        fail(409, "suggestion_applied", "That suggestion was already applied.")
    if turn.status == "stale" or turn.base_revision != draft.revision:
        fail(
            409,
            "stale_suggestion",
            "The saved draft changed after that suggestion. Refine again against the current reply.",
        )
    if body.revision != draft.revision:
        fail(409, "stale_draft", "The saved draft changed. Reload it before applying.")

    replacement = turn.suggestion.strip()
    if replacement == (draft.text or "").strip():
        # Nothing to save. Bumping the revision here would invalidate a review and an
        # approval for a change that was never made.
        await db.execute(
            update(RefinementTurn).where(RefinementTurn.id == turn.id).values(status="unchanged")
        )
        await db.commit()
        await db.refresh(draft)
        return view(draft)

    result = await db.execute(
        update(ReplyDraft)
        .where(
            ReplyDraft.id == draft.id,
            ReplyDraft.revision == body.revision,
            ReplyDraft.operation_token.is_(None),
            ReplyDraft.publication_token.is_(None),
        )
        .values(
            text=replacement,
            revision=body.revision + 1,
            quality=None,
            review_status="pending",
            human_decision="pending",
            failure={},
            operation_token=None,
            # The comment classifications still describe the same comment, so they are kept.
            token_usage=retained_usage(draft, "classification"),
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        fail(409, "draft_conflict", "Draft changed or is busy. Reload and retry.")
    await db.execute(
        update(RefinementTurn)
        .where(
            RefinementTurn.id == turn.id,
            RefinementTurn.status == "suggested",
        )
        .values(status="applied")
    )
    await db.commit()
    await db.refresh(draft)
    return view(draft)


@router.post("/{draft_id}/refine/{turn_id}/discard")
async def discard_suggestion(draft_id: str, turn_id: str, session: Csrf, db: Db):
    """Drop a suggestion without touching the draft. No model call, no revision change."""
    draft = await owned(db, session, draft_id)
    turn = await _turn(db, draft, turn_id)
    if turn.status == "applied":
        fail(409, "suggestion_applied", "That suggestion was already applied.")
    if turn.status != "discarded":
        await db.execute(
            update(RefinementTurn)
            .where(
                RefinementTurn.id == turn.id,
                RefinementTurn.status == "suggested",
            )
            .values(status="discarded")
        )
        await db.commit()
        await db.refresh(turn)
    return refinement.view(turn)
