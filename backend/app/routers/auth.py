import hmac
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from google.auth.exceptions import GoogleAuthError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from app.config import settings
from app.dependencies import get_current_session, get_db, hash_secret, require_csrf
from app.models import AppSession, ChannelOwner, YouTubeConnection
from app.security import (
    EncryptionNotConfigured,
    StoredTokenInvalid,
    decrypt_tokens,
    encrypt_tokens,
)
from app.services.google_oauth import (
    OAUTH_SCOPES,
    YOUTUBE_FORCE_SSL_SCOPE,
    YOUTUBE_PERMISSION_UPGRADE_SCOPES,
    YOUTUBE_READONLY_SCOPE,
    OAuthSetupRequired,
    create_flow,
    oauth_setup_issue,
    require_oauth_setup,
)
from app.services.youtube_api import YouTubeFailure, fetch_authorized_channel

router = APIRouter(tags=["authentication"])
DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentSession = Annotated[AppSession, Depends(get_current_session)]
CsrfSession = Annotated[AppSession, Depends(require_csrf)]


class ChannelSummary(BaseModel):
    channel_id: str
    channel_title: str
    channel_thumbnail_url: str | None
    reconnect_required: bool
    comments_scope_available: bool


class SessionStatus(BaseModel):
    csrf_token: str
    oauth_configured: bool
    setup_message: str | None
    connected: bool
    channel: ChannelSummary | None


class AuthorizationStart(BaseModel):
    authorization_url: str


class DisconnectResult(BaseModel):
    disconnected: bool
    google_revocation: Literal["revoked", "unavailable", "not_connected"]


def _cookie_settings() -> dict[str, object]:
    return {
        "key": settings.session_cookie_name,
        "httponly": True,
        "secure": settings.app_environment.casefold() == "production",
        "samesite": "lax",
        "path": "/",
        "max_age": settings.session_ttl_seconds,
    }


async def _get_or_create_session(
    request: Request, response: Response, db: AsyncSession
) -> AppSession:
    cookie = request.cookies.get(settings.session_cookie_name)
    session = await db.get(AppSession, hash_secret(cookie)) if cookie else None
    now = datetime.now(UTC)
    if session and session.expires_at:
        expiry = session.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry <= now:
            await db.delete(session)
            session = None

    if session is None:
        cookie = secrets.token_urlsafe(32)
        session = AppSession(
            session_hash=hash_secret(cookie),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        response.set_cookie(value=cookie, **_cookie_settings())
    return session


async def _connection_for_session(
    db: AsyncSession, session: AppSession, *, lock: bool = False
) -> YouTubeConnection | None:
    statement = select(YouTubeConnection).where(
        YouTubeConnection.session_hash == session.session_hash
    )
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement)


def _redirect_result(result: str) -> RedirectResponse:
    location = f"{settings.frontend_base_url.rstrip('/')}/?{urlencode({'oauth': result})}"
    return RedirectResponse(
        location,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _session_token_hash(request: Request) -> str | None:
    cookie = request.cookies.get(settings.session_cookie_name)
    return hash_secret(cookie) if cookie else None


def _valid_callback_state(session: AppSession, state: str | None) -> bool:
    if not state or not session.pending_state_hash or not session.pending_state_expires_at:
        return False
    expiry = session.pending_state_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    session_expiry = session.expires_at
    if session_expiry.tzinfo is None:
        session_expiry = session_expiry.replace(tzinfo=UTC)
    return min(expiry, session_expiry) > datetime.now(UTC) and hmac.compare_digest(
        session.pending_state_hash, sha256(state.encode("utf-8")).hexdigest()
    )


async def _clear_pending_authorization(session: AppSession, db: AsyncSession) -> None:
    session.pending_state_hash = None
    session.pending_code_verifier = None
    session.pending_state_expires_at = None
    await db.commit()


def _credential_payload(credentials: object, refresh_token: str | None) -> dict[str, str | None]:
    expiry = getattr(credentials, "expiry", None)
    return {
        "access_token": getattr(credentials, "token", None),
        "refresh_token": getattr(credentials, "refresh_token", None) or refresh_token,
        "expiry": expiry.isoformat() if expiry else None,
    }


async def _revoke_and_remove_connection(
    connection: YouTubeConnection | None, db: AsyncSession
) -> Literal["revoked", "unavailable", "not_connected"]:
    if connection is None:
        return "not_connected"

    revocation_result: Literal["revoked", "unavailable"] = "unavailable"
    try:
        tokens = decrypt_tokens(connection.encrypted_tokens)
        token = tokens.get("refresh_token") or tokens.get("access_token")
        if token:
            response = await run_in_threadpool(
                requests.post,
                "https://oauth2.googleapis.com/revoke",
                data={"token": token},
                timeout=10,
            )
            if response.status_code == 200:
                revocation_result = "revoked"
    except (
        EncryptionNotConfigured,
        StoredTokenInvalid,
        requests.RequestException,
        OSError,
        TimeoutError,
    ):
        pass

    await db.delete(connection)
    await db.commit()
    return revocation_result


@router.get("/api/session", response_model=SessionStatus)
async def session_status(
    request: Request, response: Response, db: DbSession
) -> SessionStatus:
    response.headers["Cache-Control"] = "no-store"
    session = await _get_or_create_session(request, response, db)
    issue = oauth_setup_issue()
    connection = await _connection_for_session(db, session)
    channel = None
    if connection:
        channel = ChannelSummary(
            channel_id=connection.channel_id,
            channel_title=connection.channel_title,
            channel_thumbnail_url=connection.channel_thumbnail_url,
            reconnect_required=connection.reconnect_required,
            comments_scope_available=YOUTUBE_FORCE_SSL_SCOPE in connection.scopes.split(),
        )
    return SessionStatus(
        csrf_token=session.csrf_token,
        oauth_configured=issue is None,
        setup_message=issue[1] if issue else None,
        connected=connection is not None,
        channel=channel,
    )


async def _start_authorization(
    session: AppSession,
    db: AsyncSession,
    *,
    scopes: tuple[str, ...],
    required_scope: str,
    permission_upgrade: bool,
) -> AuthorizationStart:
    try:
        require_oauth_setup()
        state = secrets.token_urlsafe(32)
        flow = create_flow(
            scopes=scopes,
            state=state,
            autogenerate_code_verifier=True,
        )
        authorization_url, returned_state = flow.authorization_url(
            state=state,
            access_type="offline",
            prompt="consent",
            include_granted_scopes="false",
        )
        if not flow.code_verifier or returned_state != state:
            raise OAuthSetupRequired("pkce_unavailable", "Secure OAuth setup is unavailable.")
        session.pending_state_hash = sha256(state.encode("utf-8")).hexdigest()
        session.pending_code_verifier = encrypt_tokens(
            {
                "code_verifier": flow.code_verifier,
                "requested_scopes": " ".join(scopes),
                "required_scope": required_scope,
                "permission_upgrade": str(permission_upgrade).lower(),
            }
        )
        session.pending_state_expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.oauth_state_ttl_seconds
        )
        await db.commit()
        return AuthorizationStart(authorization_url=authorization_url)
    except OAuthSetupRequired as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "oauth_setup_required", "message": exc.message},
        ) from None
    except (OSError, ValueError):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "oauth_setup_required",
                "message": "The OAuth client configuration could not be loaded. Check the configured external file path.",
            },
        ) from None


@router.post("/auth/google/start", response_model=AuthorizationStart)
async def google_authorization_start(
    session: CsrfSession, db: DbSession
) -> AuthorizationStart:
    connection = await _connection_for_session(db, session)
    current_scopes = set(connection.scopes.split()) if connection else set()
    known_scopes = {YOUTUBE_READONLY_SCOPE, YOUTUBE_FORCE_SSL_SCOPE}
    scopes = tuple(
        scope
        for scope in (YOUTUBE_READONLY_SCOPE, YOUTUBE_FORCE_SSL_SCOPE)
        if scope == YOUTUBE_READONLY_SCOPE or scope in current_scopes & known_scopes
    )
    return await _start_authorization(
        session,
        db,
        scopes=scopes,
        required_scope=YOUTUBE_READONLY_SCOPE,
        permission_upgrade=False,
    )


@router.post("/auth/google/upgrade", response_model=AuthorizationStart)
async def google_permission_upgrade(session: CsrfSession, db: DbSession) -> AuthorizationStart:
    connection = await _connection_for_session(db, session)
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "youtube_not_connected",
                "message": "Connect a YouTube channel before updating its permissions.",
            },
        )
    return await _start_authorization(
        session,
        db,
        scopes=YOUTUBE_PERMISSION_UPGRADE_SCOPES,
        required_scope=YOUTUBE_FORCE_SSL_SCOPE,
        permission_upgrade=True,
    )


@router.get("/auth/google/callback", name="google_oauth_callback")
async def google_authorization_callback(
    request: Request,
    db: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    session_hash = _session_token_hash(request)
    session = await db.scalar(
        select(AppSession).where(AppSession.session_hash == session_hash).with_for_update()
    ) if session_hash else None
    if session is None or not _valid_callback_state(session, state):
        return _redirect_result("invalid_state")

    try:
        verifier_data = decrypt_tokens(session.pending_code_verifier or "")
        code_verifier = verifier_data.get("code_verifier")
        requested_scopes = tuple(
            (verifier_data.get("requested_scopes") or " ".join(OAUTH_SCOPES)).split()
        )
        required_scope = verifier_data.get("required_scope") or YOUTUBE_READONLY_SCOPE
        permission_upgrade = verifier_data.get("permission_upgrade") == "true"
        if not code_verifier:
            await _clear_pending_authorization(session, db)
            return _redirect_result("invalid_state")
    except StoredTokenInvalid:
        await _clear_pending_authorization(session, db)
        return _redirect_result("invalid_state")

    await _clear_pending_authorization(session, db)
    if error:
        if error == "access_denied":
            return _redirect_result("permission_denied" if permission_upgrade else "denied")
        return _redirect_result("failed")
    if not code:
        return _redirect_result("failed")

    try:
        flow = create_flow(
            scopes=requested_scopes,
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        try:
            await run_in_threadpool(flow.fetch_token, code=code, timeout=10)
        except Warning as exc:
            # OAuthlib raises (rather than emits) a scope-change warning.
            # Keep validation local; never relax scope checks process-wide.
            if not all(hasattr(exc, name) for name in ("token", "new_scope", "old_scope")):
                return _redirect_result("failed")
            flow.oauth2session.token = exc.token

        granted = flow.oauth2session.token.get("scope", "")
        if isinstance(granted, str):
            granted_scopes = set(granted.split())
        elif isinstance(granted, (list, tuple, set)):
            granted_scopes = set(granted)
        else:
            granted_scopes = set()
        if (
            not granted_scopes
            or required_scope not in granted_scopes
            or not set(requested_scopes).issubset(granted_scopes)
        ):
            return _redirect_result("scope_required")
        credentials = flow.credentials
        channel = await fetch_authorized_channel(credentials)
        if channel is None:
            return _redirect_result("no_channel")

        connection = await _connection_for_session(db, session, lock=True)
        if permission_upgrade and (
            connection is None or connection.channel_id != channel["channel_id"]
        ):
            return _redirect_result("channel_mismatch")
        refresh_token = credentials.refresh_token
        if connection and connection.channel_id == channel["channel_id"]:
            try:
                existing = decrypt_tokens(connection.encrypted_tokens)
                refresh_token = refresh_token or existing.get("refresh_token")
            except StoredTokenInvalid:
                pass

        # The channel owner is the durable identity for this channel's drafts. Creating
        # it here, once the server has read the authorized channel, is what lets a
        # later reconnect to the same channel find the work retained under it.
        owner = await db.scalar(
            select(ChannelOwner).where(ChannelOwner.channel_id == channel["channel_id"])
        )
        if owner is None:
            db.add(ChannelOwner(channel_id=channel["channel_id"]))

        expiry = credentials.expiry
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        values = {
            **channel,
            "session_hash": session.session_hash,
            "encrypted_tokens": encrypt_tokens(_credential_payload(credentials, refresh_token)),
            "token_expires_at": expiry,
            "scopes": " ".join(sorted(granted_scopes & set(requested_scopes))),
            "reconnect_required": False,
        }
        if connection:
            for name, value in values.items():
                setattr(connection, name, value)
        else:
            db.add(YouTubeConnection(**values))
        await db.commit()
        return _redirect_result("permissions_updated" if permission_upgrade else "connected")
    except OAuthSetupRequired:
        return _redirect_result("setup_required")
    except (
        GoogleAuthError,
        OAuth2Error,
        YouTubeFailure,
        requests.RequestException,
        OSError,
        TimeoutError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return _redirect_result("failed")


@router.post("/auth/google/disconnect", response_model=DisconnectResult)
async def disconnect_google(
    session: CsrfSession, db: DbSession
) -> DisconnectResult:
    connection = await _connection_for_session(db, session, lock=True)
    revocation = await _revoke_and_remove_connection(connection, db)
    return DisconnectResult(disconnected=True, google_revocation=revocation)


@router.post("/auth/logout", status_code=204)
async def logout(
    session: CsrfSession,
    db: DbSession,
    response: Response,
) -> Response:
    connection = await _connection_for_session(db, session, lock=True)
    await _revoke_and_remove_connection(connection, db)
    await db.delete(session)
    await db.commit()
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.app_environment.casefold() == "production",
        httponly=True,
        samesite="lax",
    )
    response.status_code = 204
    return response