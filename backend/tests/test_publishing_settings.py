import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import AppSession, ChannelOwner, PublishingSettings, YouTubeConnection
from app.services.google_oauth import YOUTUBE_FORCE_SSL_SCOPE


def get_settings(workspace):
    return workspace.client.get("/api/publishing-settings")


def put_settings(workspace, mode, revision, *, confirmed=False):
    return workspace.client.put(
        "/api/publishing-settings",
        headers=workspace.headers,
        json={
            "mode": mode,
            "revision": revision,
            "confirm_automation": confirmed,
        },
    )


def test_manual_review_is_default_and_unknown_modes_are_rejected(workspace):
    assert get_settings(workspace).json() == {"mode": "manual_review", "revision": 0}

    response = put_settings(workspace, "surprise", 0, confirmed=True)
    assert response.status_code == 422
    assert get_settings(workspace).json() == {"mode": "manual_review", "revision": 0}


def test_automatic_mode_requires_confirmation_and_csrf(workspace):
    response = put_settings(workspace, "auto_on_agreement", 0)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "automation_confirmation_required"
    assert get_settings(workspace).json()["mode"] == "manual_review"

    missing_csrf = workspace.client.put(
        "/api/publishing-settings",
        json={"mode": "approve_all", "revision": 0, "confirm_automation": True},
    )
    assert missing_csrf.status_code == 403
    assert get_settings(workspace).json()["mode"] == "manual_review"


def test_mode_persists_and_stale_concurrent_change_is_rejected(workspace):
    enabled = put_settings(workspace, "auto_on_agreement", 0, confirmed=True)
    assert enabled.status_code == 200
    assert enabled.json() == {"mode": "auto_on_agreement", "revision": 1}
    assert get_settings(workspace).json() == enabled.json()

    stale = put_settings(workspace, "approve_all", 0, confirmed=True)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "publishing_settings_changed"
    assert get_settings(workspace).json() == enabled.json()

    manual = put_settings(workspace, "manual_review", 1)
    assert manual.status_code == 200
    assert manual.json() == {"mode": "manual_review", "revision": 2}


def test_settings_are_bound_to_the_authenticated_channel_owner(workspace):
    async def add_other_owner():
        async with workspace.sessions() as db:
            other = ChannelOwner(channel_id="other-channel")
            db.add(other)
            await db.flush()
            db.add(PublishingSettings(
                channel_owner_id=other.id, mode="approve_all", revision=7
            ))
            await db.commit()
            return other.id

    other_owner_id = asyncio.run(add_other_owner())
    changed = put_settings(workspace, "auto_on_agreement", 0, confirmed=True)
    assert changed.status_code == 200

    async def read_other():
        async with workspace.sessions() as db:
            return await db.get(PublishingSettings, other_owner_id)

    other = asyncio.run(read_other())
    assert other.mode == "approve_all"
    assert other.revision == 7


def test_mode_change_does_not_publish_or_rewrite_a_saved_draft(workspace, monkeypatch):
    from support import generate

    calls = []
    async def credentials(*args):
        return object()

    async def insert(*args):
        calls.append(args)
        return "unexpected"

    monkeypatch.setattr("app.routers.publication._credentials", credentials)
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)
    draft = generate(workspace)

    changed = put_settings(workspace, "approve_all", 0, confirmed=True)
    assert changed.status_code == 200
    saved = workspace.client.get(f'/api/drafts/{draft["id"]}').json()
    assert saved == draft
    assert calls == []


def test_settings_are_isolated_between_authenticated_channels(workspace):
    with TestClient(app, follow_redirects=False) as second_client:
        session = second_client.get("/api/session").json()
        headers = {
            "Origin": "http://127.0.0.1:5173",
            "X-CSRF-Token": session["csrf_token"],
        }
        async def connect_other_channel():
            async with workspace.sessions() as db:
                second_session = await db.scalar(
                    select(AppSession)
                    .outerjoin(YouTubeConnection)
                    .where(YouTubeConnection.id.is_(None))
                    .order_by(AppSession.created_at.desc())
                )
                db.add(ChannelOwner(channel_id="other-channel"))
                db.add(YouTubeConnection(
                    session_hash=second_session.session_hash,
                    channel_id="other-channel",
                    channel_title="Other",
                    uploads_playlist_id="other-uploads",
                    encrypted_tokens="test",
                    scopes=YOUTUBE_FORCE_SSL_SCOPE,
                ))
                await db.commit()

        asyncio.run(connect_other_channel())
        assert second_client.get("/api/publishing-settings").json() == {
            "mode": "manual_review", "revision": 0,
        }
        changed = second_client.put(
            "/api/publishing-settings",
            headers=headers,
            json={"mode": "approve_all", "revision": 0, "confirm_automation": True},
        )
        assert changed.status_code == 200
        assert second_client.get("/api/publishing-settings").json()["mode"] == "approve_all"
        assert get_settings(workspace).json() == {
            "mode": "manual_review", "revision": 0,
        }
