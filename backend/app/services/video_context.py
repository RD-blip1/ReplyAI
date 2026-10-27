"""Video metadata for model prompts, cached in Redis for 24 hours.

Redis is only a cache: when it is unreachable the metadata is read from YouTube directly,
so a cache outage costs one extra API read instead of failing the draft.
"""
import json
import logging
from typing import Any

from google.oauth2.credentials import Credentials
from redis.exceptions import RedisError

from app.redis_client import redis_client
from app.services.youtube_api import youtube_request

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 86400


async def get_video_context(credentials: Credentials, video_id: str) -> dict[str, str | None]:
    cache_key = f"video_context:{video_id}"
    try:
        cached = await redis_client.get(cache_key)
    except RedisError as exc:
        logger.warning("event=video_context_cache_unavailable type=%s", type(exc).__name__)
        cached = None
    if cached:
        return json.loads(cached)

    parameters: dict[str, Any] = {
        "part": "snippet",
        "id": video_id,
    }
    payload = await youtube_request(credentials, "videos", "list", **parameters)
    items = payload.get("items", [])
    if not items:
        context = {"title": None, "description": None, "channel_title": None}
    else:
        snippet = items[0].get("snippet", {})
        context = {
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "channel_title": snippet.get("channelTitle"),
        }

    try:
        await redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(context))
    except RedisError as exc:
        logger.warning("event=video_context_cache_unavailable type=%s", type(exc).__name__)
    return context
