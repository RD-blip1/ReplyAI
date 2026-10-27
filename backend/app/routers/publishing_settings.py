from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_session, get_db, require_csrf
from app.models import AppSession, PublishingSettings
from app.models.session import utc_now
from app.routers.drafts import fail
from app.routers.youtube import _owned_channel
from app.services.publishing import PublishingMode

router = APIRouter(prefix="/api/publishing-settings", tags=["publishing-settings"])
Db = Annotated[AsyncSession, Depends(get_db)]
Session = Annotated[AppSession, Depends(get_current_session)]
Csrf = Annotated[AppSession, Depends(require_csrf)]

class PublishingModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PublishingMode
    revision: int = Field(ge=0, strict=True)
    confirm_automation: StrictBool = False


def response(row: PublishingSettings | None) -> dict:
    return {
        "mode": row.mode if row else "manual_review",
        "revision": row.revision if row else 0,
    }


@router.get("")
async def get_publishing_settings(session: Session, db: Db):
    owner = await _owned_channel(db, session)
    return response(await db.get(PublishingSettings, owner.id))


@router.put("")
async def put_publishing_settings(
    body: PublishingModeUpdate,
    session: Csrf,
    db: Db,
):
    owner = await _owned_channel(db, session)
    current = await db.get(PublishingSettings, owner.id)
    current_mode = current.mode if current else "manual_review"

    if (body.mode != current_mode and body.mode != "manual_review"
            and not body.confirm_automation):
        fail(
            400,
            "automation_confirmation_required",
            "Confirm the automatic-publishing warning before enabling this mode.",
        )

    if current is None:
        if body.revision != 0:
            fail(409, "publishing_settings_changed",
                 "Publishing settings changed elsewhere. Reload before saving.")
        row = PublishingSettings(
            channel_owner_id=owner.id,
            mode=body.mode,
            revision=1,
            updated_at=utc_now(),
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            fail(409, "publishing_settings_changed",
                 "Publishing settings changed elsewhere. Reload before saving.")
        await db.refresh(row)
        return response(row)

    if body.mode == current.mode:
        if body.revision != current.revision:
            fail(409, "publishing_settings_changed",
                 "Publishing settings changed elsewhere. Reload before saving.")
        return response(current)

    result = await db.execute(
        update(PublishingSettings)
        .where(
            PublishingSettings.channel_owner_id == owner.id,
            PublishingSettings.revision == body.revision,
        )
        .values(mode=body.mode, revision=body.revision + 1, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        fail(409, "publishing_settings_changed",
             "Publishing settings changed elsewhere. Reload before saving.")
    await db.commit()
    await db.refresh(current)
    return response(current)
