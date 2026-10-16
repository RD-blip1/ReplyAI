from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_session, get_db
from app.models import AppSession, ChannelOwner, YouTubeConnection
from app.models.draft import ReplyDraft
from app.models.publication import ReplyPublication
from app.models.published_reply_guard import PublishedReplyGuard
from app.security import EncryptionNotConfigured
from app.services.google_oauth import OAuthSetupRequired, has_comment_read_scope
from app.services.youtube_api import (
    YouTubeFailure,
    credentials_for_connection,
    list_channel_comments,
    list_top_level_comments,
    list_upload_videos,
    video_belongs_to_channel,
)

router = APIRouter(prefix="/api/youtube", tags=["youtube-readonly"])
DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentSession = Annotated[AppSession, Depends(get_current_session)]


class ChannelResponse(BaseModel):
    channel_id: str
    channel_title: str
    channel_thumbnail_url: str | None


class VideoItem(BaseModel):
    video_id: str
    title: str
    thumbnail_url: str | None
    published_at: str | None
    watch_url: str


class VideoPage(BaseModel):
    items: list[VideoItem]
    next_page_token: str | None


class PublicationItem(BaseModel):
    id: str
    channel_id: str
    video_id: str
    parent_comment_id: str
    reply_comment_id: str | None
    text: str
    status: str
    created_at: str | None
    resolved_at: str | None
    failure: dict | None
    draft_revision: int
    # The comment snapshot stored on the draft, so the history is readable without a
    # second YouTube request. Null only if the draft predates the snapshot.
    comment_text: str | None = None


class PublicationPage(BaseModel):
    items: list[PublicationItem]
    total: int


class CommentItem(BaseModel):
    comment_id: str | None
    video_id: str | None = None
    video_title: str | None = None
    author_name: str
    author_thumbnail_url: str | None
    published_at: str | None
    text: str
    total_reply_count: int
    reply_status: str = "No draft"


class CommentPage(BaseModel):
    items: list[CommentItem]
    next_page_token: str | None


async def _owned_connection(db: AsyncSession, session: AppSession) -> YouTubeConnection:
    connection = await db.scalar(
        select(YouTubeConnection).where(YouTubeConnection.session_hash == session.session_hash)
    )
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "youtube_not_connected", "message": "Connect a YouTube channel first."},
        )
    return connection


async def _owned_channel(db: AsyncSession, session: AppSession) -> ChannelOwner:
    """Resolve the caller's durable channel owner from their verified connection.

    The channel is taken from the connection that a completed OAuth flow created, so it
    reflects a server-side check that the caller controls the channel rather than
    anything the client asserted. Retained drafts hang off this owner, which is why
    reconnecting the same verified channel restores access to them and a different
    channel resolves to a different owner with no access to them.
    """
    connection = await _owned_connection(db, session)
    owner = await db.scalar(
        select(ChannelOwner).where(ChannelOwner.channel_id == connection.channel_id)
    )
    if owner is None:
        # The OAuth callback creates the owner when a channel is first verified, so this
        # is a fallback for a verified channel that predates ownership or lost the row.
        # The connection is itself proof the server checked channel control, so deriving
        # the owner from it grants nothing a client could ask for by itself.
        owner = ChannelOwner(channel_id=connection.channel_id)
        db.add(owner)
        await db.flush()
    return owner


async def _credentials(connection: YouTubeConnection, db: AsyncSession):
    try:
        return await credentials_for_connection(connection, db)
    except EncryptionNotConfigured:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "oauth_setup_required",
                "message": "TOKEN_ENCRYPTION_KEY must be restored before reading this channel.",
            },
        ) from None
    except OAuthSetupRequired:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "oauth_setup_required",
                "message": "Google OAuth configuration must be restored before reading this channel.",
            },
        ) from None
    except YouTubeFailure as exc:
        if exc.code == "reconnect_required":
            connection.reconnect_required = True
            await db.commit()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


async def enrich_comment_page(db: AsyncSession, channel_id: str, page: dict) -> dict:
    comment_ids = [
        item.get("comment_id") for item in page.get("items", []) if item.get("comment_id")
    ]
    if not comment_ids:
        return page

    # Query guards
    guards = (
        (
            await db.execute(
                select(PublishedReplyGuard).where(
                    PublishedReplyGuard.channel_id == channel_id,
                    PublishedReplyGuard.parent_comment_id.in_(comment_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    guards_map = {g.parent_comment_id: g for g in guards}

    # Query drafts
    drafts = (
        (
            await db.execute(
                select(ReplyDraft).where(
                    ReplyDraft.channel_id == channel_id, ReplyDraft.comment_id.in_(comment_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    drafts_map = {d.comment_id: d for d in drafts}

    draft_ids = [d.id for d in drafts]

    # Query publications for those drafts
    pubs_map = {}
    if draft_ids:
        pubs = (
            (
                await db.execute(
                    select(ReplyPublication)
                    .where(ReplyPublication.draft_id.in_(draft_ids))
                    .order_by(ReplyPublication.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        # Keep only the most recent publication per draft (which maps 1:1 to comment here)
        for p in pubs:
            if p.parent_comment_id not in pubs_map:
                pubs_map[p.parent_comment_id] = p

    for item in page.get("items", []):
        cid = item.get("comment_id")
        if not cid:
            continue

        guard = guards_map.get(cid)
        pub = pubs_map.get(cid)
        draft = drafts_map.get(cid)

        status = "No draft"
        if guard and guard.status == "posted":
            status = "Posted"
        elif pub:
            if pub.status == "posted":
                status = "Posted"
            elif pub.status == "unknown":
                status = "Unconfirmed"
            elif pub.status == "failed":
                status = "Failed"
            elif pub.status == "sending":
                status = "Unconfirmed"
        elif guard and guard.status in ("unknown", "sending"):
            status = "Unconfirmed"
        elif draft:
            if draft.review_status != "complete":
                status = "Needs review"
            elif draft.human_decision == "approved":
                status = "Approved"
            else:
                status = "Draft ready"

        item["reply_status"] = status

    return page


@router.get("/channel", response_model=ChannelResponse)
async def connected_channel(session: CurrentSession, db: DbSession) -> ChannelResponse:
    connection = await _owned_connection(db, session)
    if connection.reconnect_required:
        raise HTTPException(
            status_code=401,
            detail={"code": "reconnect_required", "message": "Reconnect YouTube to continue."},
        )
    return ChannelResponse(
        channel_id=connection.channel_id,
        channel_title=connection.channel_title,
        channel_thumbnail_url=connection.channel_thumbnail_url,
    )


@router.get("/videos", response_model=VideoPage)
async def videos(
    session: CurrentSession,
    db: DbSession,
    page_token: Annotated[str | None, Query(max_length=1024)] = None,
) -> VideoPage:
    connection = await _owned_connection(db, session)
    credentials = await _credentials(connection, db)
    try:
        return VideoPage(**await list_upload_videos(connection, credentials, page_token))
    except YouTubeFailure as exc:
        if exc.code == "reconnect_required":
            connection.reconnect_required = True
            await db.commit()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


@router.get("/videos/{video_id}/comments", response_model=CommentPage)
async def video_comments(
    video_id: str,
    session: CurrentSession,
    db: DbSession,
    page_token: Annotated[str | None, Query(max_length=1024)] = None,
) -> CommentPage:
    connection = await _owned_connection(db, session)
    if not has_comment_read_scope(connection.scopes):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "insufficient_scope",
                "message": "Update YouTube permissions to restore comment access. The "
                "youtube.force-ssl permission is required; your existing connection "
                "will be preserved.",
            },
        )
    credentials = await _credentials(connection, db)
    try:
        belongs = await video_belongs_to_channel(connection, credentials, video_id)
        if not belongs:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "video_unavailable",
                    "message": "This video is not in the connected channel's uploads.",
                },
            )
        page_data = await list_top_level_comments(video_id, credentials, page_token)
        page_data = await enrich_comment_page(db, connection.channel_id, page_data)
        return CommentPage(**page_data)
    except YouTubeFailure as exc:
        if exc.code == "reconnect_required":
            connection.reconnect_required = True
            await db.commit()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


@router.get("/channel/comments", response_model=CommentPage)
async def channel_comments(
    session: CurrentSession,
    db: DbSession,
    page_token: Annotated[str | None, Query(max_length=1024)] = None,
) -> CommentPage:
    connection = await _owned_connection(db, session)
    if not has_comment_read_scope(connection.scopes):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "insufficient_scope",
                "message": "Update YouTube permissions to restore comment access. The "
                "youtube.force-ssl permission is required; your existing connection "
                "will be preserved.",
            },
        )
    credentials = await _credentials(connection, db)
    try:
        page_data = await list_channel_comments(connection.channel_id, credentials, page_token)
        page_data = await enrich_comment_page(db, connection.channel_id, page_data)
        return CommentPage(**page_data)
    except YouTubeFailure as exc:
        if exc.code == "reconnect_required":
            connection.reconnect_required = True
            await db.commit()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


@router.get("/publications", response_model=PublicationPage)
async def list_publications(
    session: CurrentSession,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicationPage:
    connection = await _owned_connection(db, session)

    # Get total count
    from sqlalchemy import func

    total = await db.scalar(
        select(func.count())
        .select_from(ReplyPublication)
        .where(ReplyPublication.channel_id == connection.channel_id)
    )

    # Get items
    pubs = (
        (
            await db.execute(
                select(ReplyPublication)
                .where(ReplyPublication.channel_id == connection.channel_id)
                .order_by(ReplyPublication.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    # One local join serves the stored comment snapshot for every row; no YouTube request.
    draft_ids = [p.draft_id for p in pubs]
    comment_text: dict[str, str] = {}
    if draft_ids:
        rows = (
            await db.execute(
                select(ReplyDraft.id, ReplyDraft.original_comment).where(
                    ReplyDraft.id.in_(draft_ids)
                )
            )
        ).all()
        comment_text = {row.id: row.original_comment for row in rows}

    items = []
    for p in pubs:
        items.append(
            {
                "id": p.id,
                "channel_id": p.channel_id,
                "video_id": p.video_id,
                "parent_comment_id": p.parent_comment_id,
                "reply_comment_id": p.reply_comment_id,
                "text": p.text,
                "status": p.status,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "resolved_at": p.resolved_at.isoformat() if p.resolved_at else None,
                "failure": p.failure,
                "draft_revision": p.draft_revision,
                "comment_text": comment_text.get(p.draft_id),
            }
        )

    return PublicationPage(items=items, total=total)
