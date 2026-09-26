import asyncio
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine
from app.redis_client import redis_client

router = APIRouter(tags=["health"])


class HealthReport(BaseModel):
    status: Literal["ok"]


class ReadinessChecks(BaseModel):
    database: Literal["available", "unavailable"]
    redis: Literal["available", "unavailable"]


class ReadinessReport(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


async def check_database() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, OSError, TimeoutError):
        return False


async def check_redis() -> bool:
    try:
        return bool(await redis_client.ping())
    except (RedisError, OSError, TimeoutError):
        return False


@router.get("/health", response_model=HealthReport)
async def health() -> HealthReport:
    return HealthReport(status="ok")


@router.get("/ready", response_model=ReadinessReport)
async def readiness(response: Response) -> ReadinessReport:
    database_available, redis_available = await asyncio.gather(check_database(), check_redis())
    ready = database_available and redis_available
    if not ready:
        response.status_code = 503

    return ReadinessReport(
        status="ready" if ready else "not_ready",
        checks=ReadinessChecks(
            database="available" if database_available else "unavailable",
            redis="available" if redis_available else "unavailable",
        ),
    )