import asyncio
from datetime import UTC, datetime, timedelta

from support import COMMENT, generate

from app.config import settings
from app.dependencies import hash_secret
from app.models import AppSession, ChannelOwner, ReplyDraft, ReplyPublication, YouTubeConnection
from app.services.google_oauth import YOUTUBE_FORCE_SSL_SCOPE


def test_get_publications_requires_auth(api_client):
    client, _, _ = api_client
    resp = client.get("/api/youtube/publications")
    assert resp.status_code == 401

def test_channel_comments_requires_auth(api_client):
    client, _, _ = api_client
    resp = client.get("/api/youtube/channel/comments")
    assert resp.status_code == 401

def test_get_publications_returns_empty_when_no_data(workspace):
    resp = workspace.client.get("/api/youtube/publications", headers=workspace.headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []

def test_get_publications_returns_data(workspace):
    # Generate a real draft so the foreign key constraint passes
    draft = generate(workspace, "comment")

    async def insert():
        async with workspace.sessions() as db:
            pub = ReplyPublication(
                draft_id=draft["id"], operation_id="op1", channel_id="channel",
                video_id="v1", parent_comment_id="comment", draft_revision=draft["revision"], 
                text="hello", status="posted", reply_comment_id="r1"
            )
            db.add(pub)
            await db.commit()
    
    asyncio.run(insert())
    
    resp = workspace.client.get("/api/youtube/publications", headers=workspace.headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["text"] == "hello"
    assert data["items"][0]["status"] == "posted"
    assert data["items"][0]["reply_comment_id"] == "r1"
    # The stored comment snapshot travels with the row, so history is readable without a
    # second YouTube request.
    assert data["items"][0]["comment_text"] == COMMENT
    assert "operation_token" not in data["items"][0]

def test_publication_history_preserves_failure_and_unconfirmed_statuses(workspace):
    drafts = [generate(workspace, f"comment-{index}") for index in range(3)]
    statuses = ("failed", "unknown", "sending")

    async def insert():
        async with workspace.sessions() as db:
            for draft, status in zip(drafts, statuses, strict=True):
                db.add(ReplyPublication(
                    draft_id=draft["id"], operation_id=f"operation-{status}",
                    channel_id="channel", video_id="video", parent_comment_id=draft["comment_id"],
                    draft_revision=draft["revision"], text=f"reply-{status}", status=status,
                ))
            await db.commit()

    asyncio.run(insert())

    response = workspace.client.get("/api/youtube/publications", headers=workspace.headers)
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert {item["status"] for item in response.json()["items"]} == set(statuses)

def test_publication_history_is_scoped_to_the_connected_channel(workspace):
    owned_draft = generate(workspace, "owned-comment")
    other_cookie = "second-user-session-cookie"
    other_channel = "another-channel"

    async def insert():
        async with workspace.sessions() as db:
            other_session = AppSession(
                session_hash=hash_secret(other_cookie),
                csrf_token="second-user-csrf-token",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            db.add(other_session)
            await db.flush()
            db.add(YouTubeConnection(
                session_hash=other_session.session_hash,
                channel_id=other_channel,
                channel_title="Another channel",
                uploads_playlist_id="another-uploads",
                encrypted_tokens="test",
                scopes=YOUTUBE_FORCE_SSL_SCOPE,
            ))
            other_owner = ChannelOwner(channel_id=other_channel)
            db.add(other_owner)
            await db.flush()
            other_draft = ReplyDraft(
                channel_owner_id=other_owner.id,
                channel_id=other_channel,
                video_id="other-video",
                comment_id="other-comment",
                original_comment="Another user's comment",
                text="Another channel's reply",
            )
            db.add(other_draft)
            await db.flush()
            db.add_all([
                ReplyPublication(
                    draft_id=owned_draft["id"], operation_id="owned-publication",
                    channel_id="channel", video_id="video", parent_comment_id="owned-comment",
                    draft_revision=owned_draft["revision"], text="owned reply", status="posted",
                ),
                ReplyPublication(
                    draft_id=other_draft.id, operation_id="other-channel-publication",
                    channel_id=other_channel, video_id="other-video", parent_comment_id="other-comment",
                    draft_revision=other_draft.revision, text="another channel reply", status="posted",
                ),
            ])
            await db.commit()

    asyncio.run(insert())

    first_user_cookie = workspace.client.cookies.get(settings.session_cookie_name)
    workspace.client.cookies.set(settings.session_cookie_name, other_cookie)
    other_response = workspace.client.get("/api/youtube/publications")
    assert other_response.status_code == 200
    assert [item["text"] for item in other_response.json()["items"]] == ["another channel reply"]

    workspace.client.cookies.set(settings.session_cookie_name, first_user_cookie)
    own_response = workspace.client.get("/api/youtube/publications", headers=workspace.headers)
    assert own_response.status_code == 200
    assert [item["text"] for item in own_response.json()["items"]] == ["owned reply"]

def test_publication_history_supports_limit_and_offset(workspace):
    drafts = [generate(workspace, f"comment-{index}") for index in range(2)]

    async def insert():
        async with workspace.sessions() as db:
            for index, draft in enumerate(drafts):
                db.add(ReplyPublication(
                    draft_id=draft["id"], operation_id=f"page-{index}",
                    channel_id="channel", video_id="video", parent_comment_id=draft["comment_id"],
                    draft_revision=draft["revision"], text=f"reply-{index}", status="posted",
                ))
            await db.commit()

    asyncio.run(insert())

    first = workspace.client.get(
        "/api/youtube/publications", params={"limit": 1}, headers=workspace.headers)
    second = workspace.client.get(
        "/api/youtube/publications", params={"limit": 1, "offset": 1}, headers=workspace.headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 2
    assert len(first.json()["items"]) == len(second.json()["items"]) == 1
