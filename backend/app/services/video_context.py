import json
from typing import Any

from google.oauth2.credentials import Credentials

from app.redis_client import redis_client
from app.services.youtube_api import youtube_request


async def get_video_context(credentials: Credentials, video_id: str) -> dict[str, str | None]:
    cache_key = f"video_context:{video_id}"
    cached = await redis_client.get(cache_key)
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
            "channel_title": snippet.get("channelTitle")
        }
    
    await redis_client.setex(cache_key, 86400, json.dumps(context))
    return context
