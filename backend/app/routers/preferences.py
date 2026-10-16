from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_session, get_db, require_csrf
from app.models import AppSession, ReplyPreferences
from app.models.session import utc_now
from app.routers.drafts import fail
from app.routers.youtube import _owned_channel
from app.services import reply_preferences as preference_values

router = APIRouter(prefix="/api/preferences", tags=["reply-preferences"])
Db = Annotated[AsyncSession, Depends(get_db)]
Session = Annotated[AppSession, Depends(get_current_session)]
Csrf = Annotated[AppSession, Depends(require_csrf)]


class PreferenceValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: Literal["Professional", "Friendly", "Casual"]
    reply_length: Literal["Short", "Medium", "Detailed"]
    language: Literal["English", "Hindi", "Hinglish"]
    channel_instructions: str = Field(max_length=2000)


class PreferenceUpdate(PreferenceValues):
    revision: int = Field(ge=0, strict=True)


def response(row: ReplyPreferences | None) -> dict:
    return {**preference_values.values(row), "revision": row.revision if row else 0}


@router.get("")
async def get_preferences(session: Session, db: Db):
    owner = await _owned_channel(db, session)
    row = await db.get(ReplyPreferences, owner.id)
    return response(row)


@router.put("")
async def put_preferences(
    body: PreferenceUpdate,
    session: Csrf,
    db: Db,
):
    owner = await _owned_channel(db, session)
    current = await db.get(ReplyPreferences, owner.id)
    values = body.model_dump(exclude={"revision"})

    if current is None:
        if body.revision != 0:
            fail(409, "preferences_changed", "Preferences changed elsewhere. Reload before saving.")
        row = ReplyPreferences(
            channel_owner_id=owner.id,
            **values,
            revision=1,
            updated_at=utc_now(),
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            fail(409, "preferences_changed", "Preferences changed elsewhere. Reload before saving.")
        await db.refresh(row)
        return response(row)

    result = await db.execute(
        update(ReplyPreferences)
        .where(
            ReplyPreferences.channel_owner_id == owner.id,
            ReplyPreferences.revision == body.revision,
        )
        .values(**values, revision=body.revision + 1, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        fail(409, "preferences_changed", "Preferences changed elsewhere. Reload before saving.")
    await db.commit()
    await db.refresh(current)
    return response(current)
