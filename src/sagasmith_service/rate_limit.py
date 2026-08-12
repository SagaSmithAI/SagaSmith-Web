from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError


class RateLimiterUnavailableError(RuntimeError):
    pass


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None: ...


@dataclass
class _Window:
    count: int
    resets_at: float


class MemoryRateLimiter:
    """Explicit development/test limiter; never selected as a production fallback."""

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        now = time.monotonic()
        async with self._lock:
            window = self._windows.get(key)
            if window is None or window.resets_at <= now:
                window = _Window(count=0, resets_at=now + window_seconds)
                self._windows[key] = window
            window.count += 1
            if window.count <= limit:
                return None
            return max(1, int(window.resets_at - now) + 1)


class RedisRateLimiter:
    def __init__(self, url: str) -> None:
        self.client = Redis.from_url(url, decode_responses=True)

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        try:
            async with self.client.pipeline(transaction=True) as pipeline:
                pipeline.incr(key)
                pipeline.ttl(key)
                count, ttl = await pipeline.execute()
            if int(count) == 1 or int(ttl) < 0:
                await self.client.expire(key, window_seconds)
                ttl = window_seconds
        except RedisError as exc:
            raise RateLimiterUnavailableError("rate limiter is unavailable") from exc
        if int(count) <= limit:
            return None
        return max(1, int(ttl))


def opaque_rate_key(category: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"sagasmith:rate:{category}:{digest}"
