from redis.asyncio import Redis

from app.config import settings

redis_client = Redis.from_url(
    settings.redis_url.get_secret_value(),
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)