import hmac
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import sessions
from app.models import AppSession


async def get_db() -> AsyncIterator[AsyncSession]:
    async with sessions()() as db:
        yield db


def hash_secret(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


async def get_current_session(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppSession:
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if not session_cookie:
        raise HTTPException(status_code=401, detail="session_required")

    session = await db.get(AppSession, hash_secret(session_cookie))
    if session is None:
        raise HTTPException(status_code=401, detail="session_expired")

    expiry = session.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= datetime.now(UTC):
        await db.delete(session)
        await db.commit()
        response.delete_cookie(settings.session_cookie_name, path="/")
        raise HTTPException(status_code=401, detail="session_expired")

    return session


async def require_csrf(
    request: Request,
    session: Annotated[AppSession, Depends(get_current_session)],
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> AppSession:
    origin = request.headers.get("origin")
    if origin not in settings.cors_origins or not x_csrf_token:
        raise HTTPException(status_code=403, detail="csrf_validation_failed")
    if not hmac.compare_digest(session.csrf_token, x_csrf_token):
        raise HTTPException(status_code=403, detail="csrf_validation_failed")
    return session