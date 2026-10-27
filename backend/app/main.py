import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import simulation
from app.config import settings
from app.database import dispose
from app.middleware.sensitive_query import (
    SensitiveOAuthAccessLogFilter,
    SensitiveOAuthQueryRedactionMiddleware,
)
from app.redis_client import redis_client
from app.routers.auth import router as auth_router
from app.routers.automation import review_router
from app.routers.automation import router as automation_router
from app.routers.batch import router as batch_router
from app.routers.drafts import router as drafts_router
from app.routers.health import router as health_router
from app.routers.preferences import router as preferences_router
from app.routers.publication import router as publication_router
from app.routers.publishing_settings import router as publishing_settings_router
from app.routers.reconciliation import router as reconciliation_router
from app.routers.youtube import router as youtube_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(name)s: %(message)s",
    )
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(flt, SensitiveOAuthAccessLogFilter) for flt in access_logger.filters):
        access_logger.addFilter(SensitiveOAuthAccessLogFilter())
    logger.info(
        "event=draft_workflow_engine_startup engine=%s",
        settings.draft_workflow_engine,
    )
    yield
    await redis_client.aclose()
    await dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["X-CSRF-Token"],
)
app.include_router(drafts_router)
app.include_router(batch_router)
app.include_router(automation_router)
app.include_router(review_router)
app.include_router(reconciliation_router)
app.include_router(publication_router)
app.include_router(preferences_router)
app.include_router(publishing_settings_router)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(youtube_router)
app.add_middleware(SensitiveOAuthQueryRedactionMiddleware)

# Dev-only simulation mode (REPLYAI_SIMULATION=1): fake YouTube + scripted models.
if simulation.install():
    app.include_router(simulation.router)
    logger.warning("event=simulation_mode_enabled youtube=fake models=scripted network=disabled")