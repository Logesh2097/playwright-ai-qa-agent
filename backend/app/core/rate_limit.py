"""Fixed-window rate limiting on Redis."""

import hashlib
from dataclasses import dataclass

from app.core.errors import RateLimitedError
from app.core.redis import RedisClient


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    count: int
    limit: int
    retry_after_s: int

    @property
    def exceeded(self) -> bool:
        return self.count > self.limit


def _key(scope: str, subject: str) -> str:
    # Subjects (emails, IPs) are hashed so they do not sit in Redis in clear text.
    digest = hashlib.sha256(subject.lower().encode()).hexdigest()[:32]
    return f"rl:{scope}:{digest}"


class RateLimiter:
    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def hit(self, scope: str, subject: str, *, limit: int, window_s: int) -> RateLimitStatus:
        """Count one attempt and report where the subject stands."""
        key = _key(scope, subject)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_s, nx=True)
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
        return RateLimitStatus(count=int(count), limit=limit, retry_after_s=max(int(ttl), 1))

    async def peek(self, scope: str, subject: str, *, limit: int) -> RateLimitStatus:
        """Report the current count without adding to it."""
        key = _key(scope, subject)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.ttl(key)
            raw, ttl = await pipe.execute()
        return RateLimitStatus(count=int(raw or 0), limit=limit, retry_after_s=max(int(ttl), 1))

    async def reset(self, scope: str, subject: str) -> None:
        await self._redis.delete(_key(scope, subject))

    async def enforce(
        self,
        scope: str,
        subject: str,
        *,
        limit: int,
        window_s: int,
        code: str = "rate_limited",
        message: str = "Too many requests. Please try again later.",
    ) -> None:
        status = await self.hit(scope, subject, limit=limit, window_s=window_s)
        if status.exceeded:
            raise too_many(status, code=code, message=message)


def too_many(status: RateLimitStatus, *, code: str, message: str) -> RateLimitedError:
    return RateLimitedError(message, code=code, headers={"Retry-After": str(status.retry_after_s)})
