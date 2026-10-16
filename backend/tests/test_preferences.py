import asyncio

from sqlalchemy import select
from support import COMMENT, generate

from app.dependencies import hash_secret
from app.models import AppSession, ChannelOwner, ReplyPreferences, YouTubeConnection
from app.services import llm
from app.services.google_oauth import YOUTUBE_FORCE_SSL_SCOPE


def settings_body(**overrides):
    return {
        "tone": "Professional",
        "reply_length": "Detailed",
        "language": "Hinglish",
        "channel_instructions": "Use a warm sign-off.",
        **overrides,
    }


def test_preferences_defaults_are_read_without_creating_a_row(workspace):
    w = workspace
    response = w.client.get("/api/preferences")
    assert response.status_code == 200
    assert response.json() == {
        "tone": "Friendly",
        "reply_length": "Medium",
        "language": "English",
        "channel_instructions": "",
        "revision": 0,
    }

    async def has_preferences():
        async with w.sessions() as db:
            return await db.scalar(select(ReplyPreferences))

    assert asyncio.run(has_preferences()) is None
    generate(w)
    generation = next(call for call in w.calls if call[1] is llm.Generation)
    assert generation[3]["response_preferences"] == {
        "tone": "Friendly",
        "reply_length": "Medium",
        "language": "English",
        "channel_instructions": "",
    }


def test_preferences_are_validated_authenticated_and_csrf_protected(workspace):
    w = workspace
    assert w.client.get("/api/preferences").status_code == 200
    assert w.client.put("/api/preferences", json={**settings_body(), "revision": 0}).status_code == 403
    assert w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(tone="aggressive"), "revision": 0},
    ).status_code == 422
    assert w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(channel_instructions="x" * 2001), "revision": 0},
    ).status_code == 422

    w.client.cookies.clear()
    assert w.client.get("/api/preferences").status_code == 401
    assert w.client.put(
        "/api/preferences",
        headers={"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": "valid-looking"},
        json={**settings_body(), "revision": 0},
    ).status_code == 401


def test_preferences_persist_and_reject_stale_concurrent_updates(workspace):
    w = workspace
    saved = w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(), "revision": 0},
    )
    assert saved.status_code == 200
    assert saved.json() == {**settings_body(), "revision": 1}
    updated = w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(tone="Casual"), "revision": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    stale = w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(), "revision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "preferences_changed"
    assert w.client.get("/api/preferences").json() == updated.json()


def test_preferences_remain_with_verified_channel_after_reconnect(workspace):
    w = workspace
    assert w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(), "revision": 0},
    ).status_code == 200

    async def reconnect():
        async with w.sessions() as db:
            connection = await db.scalar(select(YouTubeConnection))
            await db.delete(connection)
            session = await db.scalar(select(AppSession))
            db.add(YouTubeConnection(
                session_hash=session.session_hash,
                channel_id="channel",
                channel_title="Reconnected",
                uploads_playlist_id="uploads",
                encrypted_tokens="test",
                scopes=YOUTUBE_FORCE_SSL_SCOPE,
            ))
            await db.commit()

    asyncio.run(reconnect())
    assert w.client.get("/api/preferences").json() == {**settings_body(), "revision": 1}


def test_preferences_are_isolated_to_the_authenticated_channel(workspace):
    w = workspace
    assert w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(), "revision": 0},
    ).status_code == 200

    original_cookie = w.client.cookies.get("replyai_session")
    w.client.cookies.clear()
    csrf = w.client.get("/api/session").json()["csrf_token"]
    second_cookie = w.client.cookies.get("replyai_session")

    async def connect_different_channel():
        async with w.sessions() as db:
            session = await db.get(AppSession, hash_secret(second_cookie))
            db.add(ChannelOwner(channel_id="another-channel"))
            db.add(YouTubeConnection(
                session_hash=session.session_hash,
                channel_id="another-channel",
                channel_title="Other",
                uploads_playlist_id="uploads",
                encrypted_tokens="test",
                scopes=YOUTUBE_FORCE_SSL_SCOPE,
            ))
            await db.commit()

    asyncio.run(connect_different_channel())
    assert w.client.get("/api/preferences").json()["tone"] == "Friendly"
    second_saved = w.client.put(
        "/api/preferences",
        headers={"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": csrf},
        json={**settings_body(tone="Casual"), "revision": 0},
    )
    assert second_saved.status_code == 200
    assert second_saved.json()["tone"] == "Casual"
    w.client.cookies.set("replyai_session", original_cookie)
    assert w.client.get("/api/preferences").json() == {**settings_body(), "revision": 1}
    w.client.cookies.set("replyai_session", second_cookie)
    assert w.client.get("/api/preferences").json()["tone"] == "Casual"


def test_saved_preferences_reach_generation_regeneration_and_refinement(workspace):
    w = workspace
    updated = w.client.put(
        "/api/preferences", headers=w.headers,
        json={**settings_body(), "revision": 0},
    )
    assert updated.status_code == 200

    draft = generate(w)
    generation = next(call for call in w.calls if call[1] is llm.Generation)
    assert generation[3]["response_preferences"] == {
        "tone": "Professional",
        "reply_length": "Detailed",
        "language": "Hinglish",
        "channel_instructions": "Use a warm sign-off.",
    }
    assert generation[3]["video_id"] == "abcdefghijk"
    assert generation[3]["channel"] == {"channel_id": "channel", "title": "Test"}
    assert "channel_instructions" in generation[2]
    classification = next(call for call in w.calls if call[1] is llm.Classification)
    quality = next(call for call in w.calls if call[1] is llm.Quality)
    assert classification[3] == {"comment": COMMENT}
    assert "response_preferences" not in quality[3]

    regenerated = w.client.post(
        f'/api/drafts/{draft["id"]}/regenerate',
        headers=w.headers,
        json={"revision": draft["revision"]},
    )
    assert regenerated.status_code == 200
    generation_calls = [call for call in w.calls if call[1] is llm.Generation]
    assert len(generation_calls) == 2
    assert generation_calls[-1][3]["response_preferences"] == generation[3]["response_preferences"]

    result = w.client.post(
        f'/api/drafts/{draft["id"]}/refine',
        headers=w.headers,
        json={"revision": regenerated.json()["revision"], "instruction": "Make it more conversational."},
    )
    assert result.status_code == 200
    refinement_call = next(call for call in reversed(w.calls) if call[1] is llm.Refinement)
    assert refinement_call[3]["response_preferences"] == generation[3]["response_preferences"]
    assert refinement_call[3]["video_id"] == "abcdefghijk"
    assert refinement_call[3]["channel"]["channel_id"] == "channel"
    assert "lower-priority style guidance" in refinement_call[2]
