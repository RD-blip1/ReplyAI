import asyncio
import json
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from pydantic import SecretStr
from sqlalchemy import select

from app.config import settings
from app.dependencies import hash_secret
from app.models import AppSession, YouTubeConnection
from app.security import decrypt_tokens, encrypt_tokens
from app.services.google_oauth import (
    GOOGLE_REDIRECT_URI,
    YOUTUBE_PERMISSION_UPGRADE_SCOPES,
    YOUTUBE_READONLY_SCOPE,
)

FRONTEND_ORIGIN = "http://127.0.0.1:5173"


def configure_fake_oauth(monkeypatch, tmp_path) -> None:
    secrets_file = tmp_path / "synthetic-google-client.json"
    secrets_file.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "replyai-phase2-test.apps.googleusercontent.com",
                    "project_id": "replyai-test-only",
                    "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_secret": "not-a-real-client-secret",
                    "redirect_uris": [GOOGLE_REDIRECT_URI],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "google_oauth_client_secrets_file", str(secrets_file))
    monkeypatch.setattr(
        settings, "token_encryption_key", SecretStr(Fernet.generate_key().decode("ascii"))
    )


def csrf_headers(client) -> dict[str, str]:
    session = client.get("/api/session").json()
    return {"Origin": FRONTEND_ORIGIN, "X-CSRF-Token": session["csrf_token"]}


def start_authorization(client) -> tuple[dict[str, str], str]:
    headers = csrf_headers(client)
    response = client.post("/auth/google/start", headers=headers)
    assert response.status_code == 200
    authorization_url = response.json()["authorization_url"]
    query = parse_qs(urlparse(authorization_url).query)
    assert query["scope"] == [YOUTUBE_READONLY_SCOPE]
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["include_granted_scopes"] == ["false"]
    return headers, query["state"][0]


def start_permission_upgrade(client) -> tuple[dict[str, str], str, list[str]]:
    headers = csrf_headers(client)
    response = client.post("/auth/google/upgrade", headers=headers)
    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()["authorization_url"]).query)
    return headers, query["state"][0], query["scope"][0].split()


def add_existing_readonly_connection(
    sessions, cookie: str, scopes: str = YOUTUBE_READONLY_SCOPE
) -> str:
    old_refresh_token = "fake-existing-refresh"
    encrypted = encrypt_tokens(
        {"access_token": "fake-existing-access", "refresh_token": old_refresh_token}
    )

    async def add() -> None:
        async with sessions() as db:
            db.add(
                YouTubeConnection(
                    session_hash=hash_secret(cookie),
                    channel_id="UC_existing_channel",
                    channel_title="Existing Channel",
                    channel_thumbnail_url=None,
                    uploads_playlist_id="UU_existing_uploads",
                    encrypted_tokens=encrypted,
                    scopes=scopes,
                    reconnect_required=False,
                )
            )
            await db.commit()

    asyncio.run(add())
    return encrypted


def fake_credentials(
    access_token: str,
    refresh_token: str | None,
    *,
    expiry: datetime | None = None,
) -> Credentials:
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="replyai-test-client",
        client_secret="synthetic-test-only",
        scopes=[YOUTUBE_READONLY_SCOPE],
        expiry=expiry or datetime.now(UTC) + timedelta(hours=1),
    )


def test_missing_oauth_setup_is_described_without_breaking_health(
    api_client, monkeypatch
) -> None:
    from app.routers import health

    client, _, _ = api_client
    monkeypatch.setattr(settings, "google_oauth_client_secrets_file", "")
    monkeypatch.setattr(settings, "token_encryption_key", SecretStr(""))

    async def available() -> bool:
        return True

    monkeypatch.setattr(health, "check_database", available)
    monkeypatch.setattr(health, "check_redis", available)

    status = client.get("/api/session")
    headers = {
        "Origin": FRONTEND_ORIGIN,
        "X-CSRF-Token": status.json()["csrf_token"],
    }
    start = client.post("/auth/google/start", headers=headers)

    assert status.status_code == 200
    assert status.json()["oauth_configured"] is False
    assert "GOOGLE_OAUTH_CLIENT_SECRETS_FILE" in status.json()["setup_message"]
    assert start.status_code == 503
    assert start.json()["detail"]["code"] == "oauth_setup_required"
    assert client.get("/health").status_code == 200
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_production_session_cookie_is_secure_and_http_only(api_client, monkeypatch) -> None:
    client, _, _ = api_client
    monkeypatch.setattr(settings, "app_environment", "production")

    response = client.get("/api/session")
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    session_cookie = cookie[settings.session_cookie_name]

    assert response.status_code == 200
    assert session_cookie["httponly"] is True
    assert session_cookie["secure"] == True
    assert session_cookie["samesite"].casefold() == "lax"


def test_oauth_start_enforces_csrf_and_callback_state_is_single_use(
    api_client, monkeypatch, tmp_path
) -> None:
    client, _, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    exchange_codes: list[str] = []

    def fake_fetch_token(flow, *, code: str, timeout: int) -> None:
        exchange_codes.append(code)
        flow.oauth2session.token = {"scope": YOUTUBE_READONLY_SCOPE}

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch_token)
    creds = fake_credentials("fake-access-1", "fake-refresh-1")
    monkeypatch.setattr(Flow, "credentials", property(lambda _flow: creds))

    csrf = csrf_headers(client)
    assert client.post("/auth/google/start").status_code == 403
    assert client.post(
        "/auth/google/start", headers={**csrf, "Origin": "https://attacker.invalid"}
    ).status_code == 403

    _, state = start_authorization(client)
    assert client.cookies.get(settings.session_cookie_name)
    callback = client.get(
        "/auth/google/callback", params={"state": "not-the-state", "code": "fake-code"}
    )
    assert callback.status_code == 303
    assert "oauth=invalid_state" in callback.headers["location"]
    assert exchange_codes == []

    _, state = start_authorization(client)
    monkeypatch.setattr(
        "app.routers.auth.fetch_authorized_channel",
        lambda _credentials: asyncio.sleep(
            0,
            result={
                "channel_id": "UC_test_channel",
                "channel_title": "Test Channel",
                "channel_thumbnail_url": "https://example.invalid/channel.jpg",
                "uploads_playlist_id": "UU_test_uploads",
            },
        ),
    )
    callback = client.get(
        "/auth/google/callback", params={"state": state, "code": "synthetic-code"}
    )
    replay = client.get(
        "/auth/google/callback", params={"state": state, "code": "synthetic-code"}
    )

    assert callback.status_code == 303
    assert "oauth=connected" in callback.headers["location"]
    assert "synthetic-code" not in callback.headers["location"]
    assert replay.status_code == 303
    assert "oauth=invalid_state" in replay.headers["location"]
    assert exchange_codes == ["synthetic-code"]


def test_permission_upgrade_requests_fresh_consent_for_both_scopes(
    api_client, monkeypatch, tmp_path
) -> None:
    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    status = client.get("/api/session").json()
    assert client.post("/auth/google/upgrade").status_code == 403
    csrf = {"Origin": FRONTEND_ORIGIN, "X-CSRF-Token": status["csrf_token"]}
    assert client.post("/auth/google/upgrade", headers=csrf).status_code == 404
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    add_existing_readonly_connection(sessions, cookie)

    _, state, granted_scopes = start_permission_upgrade(client)
    _, readonly_state = start_authorization(client)

    response = client.post(
        "/auth/google/upgrade",
        headers=csrf,
    )
    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()["authorization_url"]).query)
    assert granted_scopes == list(YOUTUBE_PERMISSION_UPGRADE_SCOPES)
    assert query["scope"] == [" ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES)]
    assert query["prompt"] == ["consent"]
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["false"]
    assert query["code_challenge_method"] == ["S256"]
    assert state != readonly_state


def test_reauthentication_preserves_an_existing_comments_scope(api_client, monkeypatch, tmp_path) -> None:
    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    cookie = client.get("/api/session").cookies.get(settings.session_cookie_name)
    assert cookie
    add_existing_readonly_connection(
        sessions, cookie, scopes=" ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES)
    )

    headers = csrf_headers(client)
    response = client.post("/auth/google/start", headers=headers)
    query = parse_qs(urlparse(response.json()["authorization_url"]).query)

    assert response.status_code == 200
    assert query["scope"] == [" ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES)]
    assert query["prompt"] == ["consent"]


def test_permission_upgrade_updates_existing_grant_and_preserves_refresh_token(
    api_client, monkeypatch, tmp_path
) -> None:
    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    client.get("/api/session")
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    add_existing_readonly_connection(sessions, cookie)

    def fetch_upgrade_token(flow, *, code: str, timeout: int) -> None:
        assert timeout == 10
        assert code == "synthetic-upgrade-code"
        flow.oauth2session.token = {
            "scope": " ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES)
        }

    monkeypatch.setattr(Flow, "fetch_token", fetch_upgrade_token)
    upgraded_credentials = fake_credentials("fake-upgrade-access", None)
    monkeypatch.setattr(Flow, "credentials", property(lambda _flow: upgraded_credentials))

    async def fake_channel(_credentials):
        return {
            "channel_id": "UC_existing_channel",
            "channel_title": "Existing Channel",
            "channel_thumbnail_url": None,
            "uploads_playlist_id": "UU_existing_uploads",
        }

    monkeypatch.setattr("app.routers.auth.fetch_authorized_channel", fake_channel)
    _, state, _ = start_permission_upgrade(client)
    callback = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "synthetic-upgrade-code"},
    )

    assert callback.status_code == 303
    assert "oauth=permissions_updated" in callback.headers["location"]
    updated_status = client.get("/api/session").json()
    assert updated_status["connected"] is True
    assert updated_status["channel"]["comments_scope_available"] is True

    async def stored_connection() -> tuple[YouTubeConnection, dict[str, str | None]]:
        async with sessions() as db:
            connection = await db.scalar(select(YouTubeConnection))
            assert connection is not None
            return connection, decrypt_tokens(connection.encrypted_tokens)

    connection, tokens = asyncio.run(stored_connection())
    assert set(connection.scopes.split()) == set(YOUTUBE_PERMISSION_UPGRADE_SCOPES)
    assert tokens["access_token"] == "fake-upgrade-access"
    assert tokens["refresh_token"] == "fake-existing-refresh"
    assert connection.reconnect_required is False


def test_denied_or_partial_permission_upgrade_preserves_existing_connection(
    api_client, monkeypatch, tmp_path
) -> None:
    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    client.get("/api/session")
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    original_encrypted_tokens = add_existing_readonly_connection(sessions, cookie)

    def fetch_partial_token(flow, *, code: str, timeout: int) -> None:
        flow.oauth2session.token = {"scope": YOUTUBE_READONLY_SCOPE}

    monkeypatch.setattr(Flow, "fetch_token", fetch_partial_token)
    monkeypatch.setattr(
        Flow,
        "credentials",
        property(lambda _flow: fake_credentials("fake-partial-access", None)),
    )

    async def channel_must_not_load(_credentials):
        raise AssertionError("channel reads must not happen when the requested scope was denied")

    monkeypatch.setattr("app.routers.auth.fetch_authorized_channel", channel_must_not_load)
    _, partial_state, _ = start_permission_upgrade(client)
    partial = client.get(
        "/auth/google/callback",
        params={"state": partial_state, "code": "synthetic-partial-code"},
    )
    assert "oauth=scope_required" in partial.headers["location"]

    _, denied_state, _ = start_permission_upgrade(client)
    denied = client.get(
        "/auth/google/callback",
        params={"state": denied_state, "error": "access_denied"},
    )
    assert "oauth=permission_denied" in denied.headers["location"]

    async def stored_connection() -> YouTubeConnection:
        async with sessions() as db:
            connection = await db.scalar(select(YouTubeConnection))
            assert connection is not None
            return connection

    connection = asyncio.run(stored_connection())
    assert connection.encrypted_tokens == original_encrypted_tokens
    assert connection.scopes == YOUTUBE_READONLY_SCOPE
    assert connection.reconnect_required is False
    updated_status = client.get("/api/session").json()
    assert updated_status["connected"] is True
    assert updated_status["channel"]["comments_scope_available"] is False


def test_expired_state_and_user_denial_are_handled_without_code_exchange(
    api_client, monkeypatch, tmp_path
) -> None:
    client, _, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "oauth_state_ttl_seconds", -1)

    def exchange_must_not_run(_flow, **_kwargs):
        raise AssertionError("authorization code exchange must not run")

    monkeypatch.setattr(Flow, "fetch_token", exchange_must_not_run)
    _, expired_state = start_authorization(client)
    expired = client.get(
        "/auth/google/callback", params={"state": expired_state, "code": "synthetic-code"}
    )
    assert expired.status_code == 303
    assert "oauth=invalid_state" in expired.headers["location"]

    monkeypatch.setattr(settings, "oauth_state_ttl_seconds", 600)
    _, denied_state = start_authorization(client)
    denied = client.get(
        "/auth/google/callback",
        params={"state": denied_state, "error": "access_denied"},
    )
    replay = client.get(
        "/auth/google/callback",
        params={"state": denied_state, "error": "access_denied"},
    )
    assert "oauth=denied" in denied.headers["location"]
    assert "oauth=invalid_state" in replay.headers["location"]


def test_tokens_are_encrypted_refresh_token_is_preserved_and_expired_access_refreshes(
    api_client, monkeypatch, tmp_path
) -> None:
    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    credentials_to_return = iter(
        [
            fake_credentials("fake-access-initial", "fake-refresh-preserve"),
            fake_credentials("fake-access-reconnect", None),
        ]
    )
    exchange_codes: list[str] = []

    def fake_fetch_token(flow, *, code: str, timeout: int) -> None:
        exchange_codes.append(code)
        flow.oauth2session.token = {"scope": YOUTUBE_READONLY_SCOPE}

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch_token)
    monkeypatch.setattr(
        Flow, "credentials", property(lambda _flow: next(credentials_to_return))
    )

    async def fake_channel(_credentials):
        return {
            "channel_id": "UC_test_channel",
            "channel_title": "Test Channel",
            "channel_thumbnail_url": "https://example.invalid/channel.jpg",
            "uploads_playlist_id": "UU_test_uploads",
        }

    monkeypatch.setattr("app.routers.auth.fetch_authorized_channel", fake_channel)

    for code in ("synthetic-first-code", "synthetic-reconnect-code"):
        _, state = start_authorization(client)
        response = client.get(
            "/auth/google/callback", params={"state": state, "code": code}
        )
        assert response.status_code == 303
        assert "oauth=connected" in response.headers["location"]

    async def stored_tokens() -> tuple[YouTubeConnection, dict[str, str | None]]:
        async with sessions() as db:
            connection = await db.scalar(select(YouTubeConnection))
            assert connection is not None
            encrypted = connection.encrypted_tokens
            return connection, decrypt_tokens(encrypted)

    connection, tokens = asyncio.run(stored_tokens())
    assert "fake-access-reconnect" not in connection.encrypted_tokens
    assert "fake-refresh-preserve" not in connection.encrypted_tokens
    assert tokens["access_token"] == "fake-access-reconnect"
    assert tokens["refresh_token"] == "fake-refresh-preserve"
    assert exchange_codes == ["synthetic-first-code", "synthetic-reconnect-code"]

    expired = datetime.now(UTC) - timedelta(minutes=5)

    async def expire_tokens() -> None:
        async with sessions() as db:
            row = await db.scalar(select(YouTubeConnection))
            assert row is not None
            current = decrypt_tokens(row.encrypted_tokens)
            current["expiry"] = expired.isoformat()
            row.encrypted_tokens = encrypt_tokens(current)
            row.token_expires_at = expired
            await db.commit()

    asyncio.run(expire_tokens())
    refresh_calls: list[bool] = []

    def fake_refresh(credentials, _request) -> None:
        refresh_calls.append(True)
        credentials.token = "fake-access-refreshed"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    async def empty_uploads(_credentials, _resource, _method, **_params):
        return {"items": []}

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    monkeypatch.setattr("app.services.youtube_api.youtube_request", empty_uploads)
    response = client.get("/api/youtube/videos")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_page_token": None}
    assert refresh_calls == [True]
    _, refreshed = asyncio.run(stored_tokens())
    assert refreshed["access_token"] == "fake-access-refreshed"
    assert refreshed["refresh_token"] == "fake-refresh-preserve"


def test_session_isolation_and_comments_require_an_owned_video(
    api_client, monkeypatch, tmp_path
) -> None:
    client, _, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    creds = fake_credentials("fake-access-owner", "fake-refresh-owner")
    monkeypatch.setattr(Flow, "fetch_token", lambda flow, **_kwargs: setattr(
        flow.oauth2session, "token", {"scope": " ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES)}
    ))
    monkeypatch.setattr(Flow, "credentials", property(lambda _flow: creds))

    async def fake_channel(_credentials):
        return {
            "channel_id": "UC_owner_channel",
            "channel_title": "Owner Channel",
            "channel_thumbnail_url": None,
            "uploads_playlist_id": "UU_owner_uploads",
        }

    async def fake_credentials_for_connection(_connection, _db):
        return creds

    async def not_owned(_connection, _credentials, _video_id):
        return False

    async def comments_must_not_load(*_args, **_kwargs):
        raise AssertionError("comments were requested for a video outside the uploads playlist")

    monkeypatch.setattr("app.routers.auth.fetch_authorized_channel", fake_channel)
    monkeypatch.setattr(
        "app.routers.youtube.credentials_for_connection", fake_credentials_for_connection
    )
    monkeypatch.setattr("app.routers.youtube.video_belongs_to_channel", not_owned)
    monkeypatch.setattr("app.routers.youtube.list_top_level_comments", comments_must_not_load)

    status = client.get("/api/session").json()
    _, state = start_authorization(client)
    callback = client.get(
        "/auth/google/callback", params={"state": state, "code": "synthetic-owner-code"}
    )
    assert callback.status_code == 303
    assert client.get("/api/youtube/channel").json()["channel_title"] == "Owner Channel"

    _, upgrade_state, _ = start_permission_upgrade(client)
    upgraded = client.get(
        "/auth/google/callback", params={"state": upgrade_state, "code": "synthetic-upgrade"}
    )
    assert "oauth=permissions_updated" in upgraded.headers["location"]

    not_owned_response = client.get("/api/youtube/videos/abcdefghijk/comments")
    assert not_owned_response.status_code == 404
    assert not_owned_response.json()["detail"]["code"] == "video_unavailable"

    client.cookies.clear()
    second_session = client.get("/api/session").json()
    assert second_session["connected"] is False
    assert second_session["csrf_token"] != status["csrf_token"]
    assert client.get("/api/youtube/channel").status_code == 404


def _add_scoped_connection(sessions, cookie: str, scopes: str) -> None:
    async def add_connection() -> None:
        async with sessions() as db:
            db.add(
                YouTubeConnection(
                    session_hash=hash_secret(cookie),
                    channel_id="UC_scope_test",
                    channel_title="Scope Test",
                    channel_thumbnail_url=None,
                    uploads_playlist_id="UU_scope_test",
                    encrypted_tokens="encrypted-test-placeholder",
                    scopes=scopes,
                )
            )
            await db.commit()

    asyncio.run(add_connection())


def test_force_ssl_scope_can_read_public_comments(
    api_client, monkeypatch
) -> None:
    client, sessions, _ = api_client
    csrf_headers(client)
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    _add_scoped_connection(sessions, cookie, " ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES))

    calls: list[tuple] = []

    async def usable_credentials(_connection, _db):
        return object()

    async def owned_video(_connection, _credentials, _video_id):
        return True

    async def public_comments(_video_id, _credentials, _page_token):
        calls.append((_video_id, _page_token))
        return {"items": [], "next_page_token": None}

    monkeypatch.setattr("app.routers.youtube.credentials_for_connection", usable_credentials)
    monkeypatch.setattr("app.routers.youtube.video_belongs_to_channel", owned_video)
    monkeypatch.setattr("app.routers.youtube.list_top_level_comments", public_comments)

    response = client.get("/api/youtube/videos/abcdefghijk/comments")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_page_token": None}
    assert calls == [("abcdefghijk", None)]


@pytest.mark.parametrize(
    "scopes",
    [YOUTUBE_READONLY_SCOPE, "https://www.googleapis.com/auth/userinfo.email"],
)
def test_connection_without_comment_scope_is_refused_before_google(
    api_client, monkeypatch, scopes
) -> None:
    client, sessions, _ = api_client
    csrf_headers(client)
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    _add_scoped_connection(sessions, cookie, scopes)

    async def credentials_must_not_run(_connection, _db):
        raise AssertionError("no comment scope, so no Google call may be made")

    monkeypatch.setattr("app.routers.youtube.credentials_for_connection", credentials_must_not_run)

    response = client.get("/api/youtube/videos/abcdefghijk/comments")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"


def test_provider_scope_denial_does_not_mark_connection_for_reconnect(
    api_client, monkeypatch
) -> None:
    from app.services.youtube_api import YouTubeFailure

    client, sessions, _ = api_client
    csrf_headers(client)
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    _add_scoped_connection(sessions, cookie, " ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES))

    async def usable_credentials(_connection, _db):
        return object()

    async def owned_video(_connection, _credentials, _video_id):
        return True

    async def denied_comments(_video_id, _credentials, _page_token):
        raise YouTubeFailure(
            "insufficient_scope",
            "Google denied this comment request.",
            403,
        )

    monkeypatch.setattr("app.routers.youtube.credentials_for_connection", usable_credentials)
    monkeypatch.setattr("app.routers.youtube.video_belongs_to_channel", owned_video)
    monkeypatch.setattr("app.routers.youtube.list_top_level_comments", denied_comments)

    response = client.get("/api/youtube/videos/abcdefghijk/comments")
    status = client.get("/api/session")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"
    assert status.json()["channel"]["reconnect_required"] is False



def test_disconnect_revokes_google_token_and_removes_local_connection(
    api_client, monkeypatch
) -> None:
    client, sessions, _ = api_client
    monkeypatch.setattr(
        settings, "token_encryption_key", SecretStr(Fernet.generate_key().decode("ascii"))
    )
    headers = csrf_headers(client)
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie

    async def add_connection() -> None:
        async with sessions() as db:
            assert await db.get(AppSession, hash_secret(cookie)) is not None
            db.add(
                YouTubeConnection(
                    session_hash=hash_secret(cookie),
                    channel_id="UC_disconnect_test",
                    channel_title="Disconnect Test",
                    channel_thumbnail_url=None,
                    uploads_playlist_id="UU_disconnect_test",
                    encrypted_tokens=encrypt_tokens(
                        {"access_token": "access-for-test", "refresh_token": "refresh-for-test"}
                    ),
                    scopes=YOUTUBE_READONLY_SCOPE,
                )
            )
            await db.commit()

    asyncio.run(add_connection())
    revoked_tokens: list[str] = []

    class RevocationResponse:
        status_code = 200

    def fake_revoke(_url, *, data, timeout):
        revoked_tokens.append(data["token"])
        assert timeout == 10
        return RevocationResponse()

    monkeypatch.setattr("app.routers.auth.requests.post", fake_revoke)
    response = client.post("/auth/google/disconnect", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"disconnected": True, "google_revocation": "revoked"}
    assert revoked_tokens == ["refresh-for-test"]
    assert client.get("/api/youtube/channel").status_code == 404


def test_disconnect_still_deletes_local_tokens_when_encryption_key_is_unavailable(
    api_client, monkeypatch
) -> None:
    client, sessions, _ = api_client
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(settings, "token_encryption_key", key)
    headers = csrf_headers(client)
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie

    async def add_connection() -> None:
        async with sessions() as db:
            db.add(
                YouTubeConnection(
                    session_hash=hash_secret(cookie),
                    channel_id="UC_keyless_test",
                    channel_title="Keyless Test",
                    channel_thumbnail_url=None,
                    uploads_playlist_id="UU_keyless_test",
                    encrypted_tokens=encrypt_tokens(
                        {"access_token": "access-for-test", "refresh_token": "refresh-for-test"}
                    ),
                    scopes=YOUTUBE_READONLY_SCOPE,
                )
            )
            await db.commit()

    asyncio.run(add_connection())
    monkeypatch.setattr(settings, "token_encryption_key", SecretStr(""))
    response = client.post("/auth/google/disconnect", headers=headers)

    async def connection_exists() -> bool:
        async with sessions() as db:
            return await db.scalar(select(YouTubeConnection)) is not None

    assert response.status_code == 200
    assert response.json() == {"disconnected": True, "google_revocation": "unavailable"}
    assert asyncio.run(connection_exists()) is False


def test_invalid_grant_requires_reconnect(api_client, monkeypatch, tmp_path) -> None:
    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    csrf_headers(client)
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    expired = datetime.now(UTC) - timedelta(minutes=5)

    async def add_expired_connection() -> None:
        async with sessions() as db:
            db.add(
                YouTubeConnection(
                    session_hash=hash_secret(cookie),
                    channel_id="UC_revoked_test",
                    channel_title="Revoked Test",
                    channel_thumbnail_url=None,
                    uploads_playlist_id="UU_revoked_test",
                    encrypted_tokens=encrypt_tokens(
                        {
                            "access_token": "expired-access",
                            "refresh_token": "revoked-refresh",
                            "expiry": expired.isoformat(),
                        }
                    ),
                    token_expires_at=expired,
                    scopes=YOUTUBE_READONLY_SCOPE,
                )
            )
            await db.commit()

    asyncio.run(add_expired_connection())

    def revoked_refresh(_credentials, _request) -> None:
        raise RefreshError({"error": "invalid_grant"})

    monkeypatch.setattr(Credentials, "refresh", revoked_refresh)
    response = client.get("/api/youtube/videos")
    status = client.get("/api/session")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "reconnect_required"
    assert status.json()["channel"]["reconnect_required"] is True

@pytest.mark.parametrize(
    "granted,channel_id,expected",
    [
        (YOUTUBE_READONLY_SCOPE, "UC_existing_channel", "scope_required"),
        (None, "UC_existing_channel", "scope_required"),
        (" ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES), "UC_existing_channel", "permissions_updated"),
        (" ".join(YOUTUBE_PERMISSION_UPGRADE_SCOPES), "UC_other_channel", "channel_mismatch"),
    ],
)
def test_upgrade_with_real_token_parser(
    api_client, monkeypatch, tmp_path, granted, channel_id, expected
):
    import requests

    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)
    client.get("/api/session")
    cookie = client.cookies.get(settings.session_cookie_name)
    original = add_existing_readonly_connection(sessions, cookie)

    def token_response(_session, request, **_kwargs):
        assert request.url == "https://oauth2.googleapis.com/token"
        assert b"code_verifier=" in (request.body.encode() if isinstance(request.body, str) else request.body)
        payload = {"access_token": "synthetic-upgrade-access", "token_type": "Bearer", "expires_in": 3600}
        if granted is not None:
            payload["scope"] = granted
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests.sessions.Session, "send", token_response)

    async def channel(_credentials):
        assert expected != "scope_required"
        return {
            "channel_id": channel_id, "channel_title": "Existing Channel",
            "channel_thumbnail_url": None, "uploads_playlist_id": "UU_existing_uploads",
        }

    monkeypatch.setattr("app.routers.auth.fetch_authorized_channel", channel)
    _, state, _ = start_permission_upgrade(client)
    response = client.get("/auth/google/callback", params={"state": state, "code": "synthetic"})
    assert response.status_code == 303
    assert f"oauth={expected}" in response.headers["location"]
    assert "oauth=invalid_state" in client.get(
        "/auth/google/callback", params={"state": state, "code": "synthetic"}
    ).headers["location"]

    async def stored():
        async with sessions() as db:
            return await db.scalar(select(YouTubeConnection))

    connection = asyncio.run(stored())
    assert connection.channel_id == "UC_existing_channel"
    if expected != "permissions_updated":
        assert connection.encrypted_tokens == original
        assert connection.scopes == YOUTUBE_READONLY_SCOPE
    else:
        assert set(connection.scopes.split()) == set(YOUTUBE_PERMISSION_UPGRADE_SCOPES)
        assert decrypt_tokens(connection.encrypted_tokens)["refresh_token"] == "fake-existing-refresh"


def test_upgrade_state_is_bound_to_session_and_session_expiry(api_client, monkeypatch, tmp_path):
    client, sessions, _ = api_client
    configure_fake_oauth(monkeypatch, tmp_path)
    client.get("/api/session")
    cookie = client.cookies.get(settings.session_cookie_name)
    add_existing_readonly_connection(sessions, cookie)
    _, state, _ = start_permission_upgrade(client)
    client.cookies.clear()
    headers = csrf_headers(client)
    assert client.post("/auth/google/upgrade", headers=headers).status_code == 404
    assert "oauth=invalid_state" in client.get(
        "/auth/google/callback", params={"state": state, "code": "synthetic"}
    ).headers["location"]
    client.cookies.clear()
    client.cookies.set(settings.session_cookie_name, cookie)

    async def expire():
        async with sessions() as db:
            session = await db.get(AppSession, hash_secret(cookie))
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()

    asyncio.run(expire())
    assert "oauth=invalid_state" in client.get(
        "/auth/google/callback", params={"state": state, "code": "synthetic"}
    ).headers["location"]
