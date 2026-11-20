"""Redis client factory."""

from redis.asyncio import Redis

from app.core.config import Settings

type RedisClient = Redis


def create_redis(settings: Settings) -> RedisClient:
    """Create a client with a lazily connected pool; no I/O happens here."""
    client: RedisClient = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.readiness_timeout_s,
        health_check_interval=30,
    )
    return client
