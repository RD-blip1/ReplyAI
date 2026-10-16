"""Retained work outlives the session and the credential that created it.

The failure these guard against is silent destruction: drafts used to cascade from both
the browser session and the OAuth connection, so an expiry, a disconnect, or a logout
removed a user's drafts and usage history. Ownership now lives on the channel, reached
only through a server-side OAuth check, so credentials can come and go without taking
the work with them.

Every provider and YouTube call here is mocked, and nothing connects to a real channel.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support import generate

from app.main import app
from app.models import AppSession, ChannelOwner, ReplyDraft, YouTubeConnection


@pytest.fixture
def refined_draft(workspace):
    w = workspace
    return w, generate(w)


def rows(w, draft_id=None):
    async def read():
        async with w.sessions() as db:
            drafts = (await db.execute(select(ReplyDraft))).scalars().all()
            return drafts
    return asyncio.run(read())


def usage_of(w, draft):
    return w.client.get(f'/api/drafts/{draft["id"]}/usage').json()


def test_disconnect_keeps_drafts_and_usage_but_ends_access(workspace):
    """Disconnecting removes the credential and access, and keeps the work."""
    w = workspace
    draft = generate(w)
    before = usage_of(w, draft)
    assert before["totals"]["requests"] > 0

    assert w.client.post("/auth/google/disconnect", headers=w.headers).status_code == 200

    # Access is gone because there is no verified connection any more...
    assert w.client.get(f'/api/drafts/{draft["id"]}').status_code == 404
    assert w.client.get(f'/api/drafts/{draft["id"]}/usage').status_code == 404
    # ...but the records themselves are untouched.
    assert len(rows(w)) == 1
    async def usage_count():
        async with w.sessions() as db:
            from app.models import DraftUsage
            return len((await db.execute(select(DraftUsage))).scalars().all())
    assert asyncio.run(usage_count()) == before["totals"]["requests"]


def test_logout_keeps_drafts_and_usage(workspace):
    w = workspace
    draft = generate(w)
    before = usage_of(w, draft)["totals"]["requests"]

    assert w.client.post("/auth/logout", headers=w.headers).status_code == 204

    assert w.client.get(f'/api/drafts/{draft["id"]}').status_code in (401, 404)
    assert len(rows(w)) == 1
    async def usage_count():
        async with w.sessions() as db:
            from app.models import DraftUsage
            return len((await db.execute(select(DraftUsage))).scalars().all())
    assert asyncio.run(usage_count()) == before


def test_session_expiry_does_not_delete_retained_work(workspace):
    """The cascade that used to destroy drafts on expiry is gone."""
    w = workspace
    draft = generate(w)

    async def expire():
        async with w.sessions() as db:
            session = await db.scalar(select(AppSession))
            session.expires_at = session.created_at  # already expired
            await db.commit()
            await db.delete(session)  # what expiry cleanup does
            await db.commit()
    asyncio.run(expire())

    assert len(rows(w)) == 1, "expiring a session must not delete its drafts"
    assert rows(w)[0].id == draft["id"]


def test_reconnecting_the_same_verified_channel_restores_access(workspace):
    """Same channel, brand new session and credential: the work is still there."""
    w = workspace
    draft = generate(w)
    before = usage_of(w, draft)
    w.client.post("/auth/logout", headers=w.headers)
    assert w.client.get(f'/api/drafts/{draft["id"]}').status_code in (401, 404)

    fresh = TestClient(app)
    fresh.get("/api/session")
    headers = {"Origin": "http://127.0.0.1:5173",
               "X-CSRF-Token": fresh.get("/api/session").json()["csrf_token"]}

    async def reconnect():
        async with w.sessions() as db:
            session = await db.scalar(
                select(AppSession).where(AppSession.session_hash.not_in(
                    [s.session_hash for s in (await db.execute(
                        select(YouTubeConnection))).scalars().all()])))
            db.add(YouTubeConnection(session_hash=session.session_hash,
                                    channel_id="channel", channel_title="Test",
                                    uploads_playlist_id="uploads", encrypted_tokens="test",
                                    scopes="https://www.googleapis.com/auth/youtube.readonly"))
            await db.commit()
    asyncio.run(reconnect())

    restored = fresh.get(f'/api/drafts/{draft["id"]}')
    assert restored.status_code == 200, restored.text
    assert restored.json()["text"] == draft["text"]
    assert fresh.get(f'/api/drafts/{draft["id"]}/usage').json()["totals"] == before["totals"]
    assert fresh.get("/api/drafts", params={
        "video_id": "abcdefghijk", "comment_id": "comment"}).json()["id"] == draft["id"]
    assert headers  # the fresh client is usable


def test_a_different_channel_cannot_reach_retained_work(workspace):
    """Knowing a draft id is not access; the verified channel decides."""
    w = workspace
    draft = generate(w)

    other = TestClient(app)
    other.get("/api/session")
    other_headers = {"Origin": "http://127.0.0.1:5173",
                     "X-CSRF-Token": other.get("/api/session").json()["csrf_token"]}

    async def connect_other():
        async with w.sessions() as db:
            connected = {c.session_hash for c in (
                await db.execute(select(YouTubeConnection))).scalars().all()}
            fresh = next(s for s in (await db.execute(select(AppSession))).scalars().all()
                         if s.session_hash not in connected)
            db.add(YouTubeConnection(session_hash=fresh.session_hash,
                                    channel_id="other-channel", channel_title="Other",
                                    uploads_playlist_id="uploads", encrypted_tokens="test",
                                    scopes="https://www.googleapis.com/auth/youtube.readonly"))
            await db.commit()
    asyncio.run(connect_other())

    assert other.get(f'/api/drafts/{draft["id"]}').status_code == 404
    assert other.get(f'/api/drafts/{draft["id"]}/usage').status_code == 404
    assert other.get("/api/drafts", params={
        "video_id": "abcdefghijk", "comment_id": "comment"}).json() is None
    for action, body in (("edit", {"text": "edit"}), ("review", {}),
                         ("regenerate", {}), ("decision", {"decision": "rejected"})):
        assert other.post(f'/api/drafts/{draft["id"]}/{action}', headers=other_headers,
                          json={"revision": draft["revision"], **body}).status_code == 404
    # The owner's own access is unaffected by the attempt.
    assert w.client.get(f'/api/drafts/{draft["id"]}').json() == draft


def test_unauthenticated_caller_cannot_reach_retained_work(workspace):
    w = workspace
    draft = generate(w)
    stranger = TestClient(app)
    stranger.get("/api/session")
    assert stranger.get(f'/api/drafts/{draft["id"]}').status_code in (401, 404)
    assert stranger.get(f'/api/drafts/{draft["id"]}/usage').status_code in (401, 404)


def test_permanent_deletion_is_explicit_and_removes_the_work(workspace):
    """Disconnect keeps work; only the explicit delete action destroys it."""
    w = workspace
    draft = generate(w)
    assert usage_of(w, draft)["totals"]["requests"] > 0

    assert w.client.post("/auth/google/disconnect", headers=w.headers).status_code == 200
    assert len(rows(w)) == 1, "disconnect alone must not delete"

    async def reconnect():
        async with w.sessions() as db:
            session = await db.scalar(select(AppSession))
            db.add(YouTubeConnection(session_hash=session.session_hash,
                                    channel_id="channel", channel_title="Test",
                                    uploads_playlist_id="uploads", encrypted_tokens="test",
                                    scopes="https://www.googleapis.com/auth/youtube.readonly"))
            await db.commit()
    asyncio.run(reconnect())
    assert w.client.get(f'/api/drafts/{draft["id"]}').status_code == 200

    assert w.client.delete("/api/drafts", headers=w.headers).status_code == 204

    assert rows(w) == [], "permanent deletion must remove the drafts"
    async def everything_gone():
        async with w.sessions() as db:
            from app.models import DraftUsage
            return (len((await db.execute(select(ReplyDraft))).scalars().all()),
                    len((await db.execute(select(DraftUsage))).scalars().all()),
                    len((await db.execute(select(ChannelOwner))).scalars().all()))
    assert asyncio.run(everything_gone()) == (0, 0, 0)
    assert w.client.get(f'/api/drafts/{draft["id"]}').status_code == 404
    # The credential is untouched by deleting retained work.
    assert w.client.get("/api/drafts/setup").json()["configured"] is True


def test_permanent_deletion_requires_authentication_and_csrf(workspace):
    w = workspace
    generate(w)
    stranger = TestClient(app)
    stranger_csrf = {"Origin": "http://127.0.0.1:5173",
                     "X-CSRF-Token": stranger.get("/api/session").json()["csrf_token"]}
    # Authenticated but holding no verified channel: nothing to delete, and no reach
    # into the owner's work through this route.
    assert stranger.delete("/api/drafts", headers=stranger_csrf).status_code == 404
    assert w.client.delete("/api/drafts").status_code == 403, "no CSRF token"
    assert len(rows(w)) == 1, "a rejected delete must not remove anything"


def test_ownership_is_per_channel_not_per_session(workspace):
    """Two sessions that both verified the same channel share one owner."""
    w = workspace
    generate(w)
    second = TestClient(app)
    second.get("/api/session")
    async def connect_second():
        async with w.sessions() as db:
            connected = {c.session_hash for c in (
                await db.execute(select(YouTubeConnection))).scalars().all()}
            fresh = next(s for s in (await db.execute(select(AppSession))).scalars().all()
                         if s.session_hash not in connected)
            db.add(YouTubeConnection(session_hash=fresh.session_hash,
                                    channel_id="channel", channel_title="Test",
                                    uploads_playlist_id="uploads", encrypted_tokens="test",
                                    scopes="https://www.googleapis.com/auth/youtube.readonly"))
            await db.commit()
    asyncio.run(connect_second())
    async def owner_count():
        async with w.sessions() as db:
            return len((await db.execute(select(ChannelOwner))).scalars().all())
    assert asyncio.run(owner_count()) == 1


def test_permanent_deletion_also_removes_the_conversation(refined_draft):
    """The conversation belongs to the draft, so it goes when the draft is deleted."""
    w, draft = refined_draft
    w.client.post(f'/api/drafts/{draft["id"]}/refine', headers=w.headers,
                  json={"revision": draft["revision"], "instruction": "make it warmer"})

    async def turn_count():
        async with w.sessions() as db:
            from app.models import RefinementTurn
            return len((await db.execute(select(RefinementTurn))).scalars().all())
    assert asyncio.run(turn_count()) == 1

    assert w.client.delete("/api/drafts", headers=w.headers).status_code == 204
    assert rows(w) == []
    assert asyncio.run(turn_count()) == 0, "the conversation must not outlive the draft"


def test_disconnect_and_expiry_keep_the_conversation(refined_draft):
    """Retention covers the conversation as well as the draft and its usage."""
    w, draft = refined_draft
    w.client.post(f'/api/drafts/{draft["id"]}/refine', headers=w.headers,
                  json={"revision": draft["revision"], "instruction": "make it warmer"})

    async def turn_count():
        async with w.sessions() as db:
            from app.models import RefinementTurn
            return len((await db.execute(select(RefinementTurn))).scalars().all())

    assert w.client.post("/auth/google/disconnect", headers=w.headers).status_code == 200
    assert asyncio.run(turn_count()) == 1
    w.client.post("/auth/logout", headers=w.headers)
    assert asyncio.run(turn_count()) == 1
    assert rows(w), "the draft is retained too"
