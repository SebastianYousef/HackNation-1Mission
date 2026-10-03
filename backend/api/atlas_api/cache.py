"""Shared key/value store: Redis when REDIS_URL is set, else an in-process TTL dict (dev only).

Used for the GET response cache, AI rate-limit counters and job status. With
Redis every replica sees the same state, which is what keeps replicas stateless.

Redis failures fail open (cache miss, no rate limit). Short socket timeouts plus a
small circuit breaker keep a hanging Redis cheap: after BREAKER_FAILURES failures in
a row the store skips Redis for BREAKER_COOLDOWN_SECONDS, then tries one op again.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger("atlas_api.cache")

# Redis is on the same LAN/host: a healthy op takes ~1 ms, so 0.5 s means it is gone.
REDIS_TIMEOUT_SECONDS = 0.5
BREAKER_FAILURES = 2
BREAKER_COOLDOWN_SECONDS = 5.0


class Store:
    def __init__(self, redis: Redis | None):
        self.redis = redis
        self._mem: dict[str, tuple[float, bytes]] = {}
        self._failures = 0
        self._open_until = 0.0  # monotonic; Redis is skipped until then

    @classmethod
    def from_url(cls, url: str | None, timeout: float = REDIS_TIMEOUT_SECONDS) -> "Store":
        return cls(Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout) if url else None)

    async def _redis(self, op: Callable[[], Awaitable[Any]], default: Any) -> Any:
        """Run one Redis op through the breaker; `default` when Redis fails or is skipped."""
        now = time.monotonic()
        if now < self._open_until:
            return default
        try:
            result = await op()
        except Exception as exc:  # the store must never break a request
            self._failures += 1
            if self._failures >= BREAKER_FAILURES:
                if self._failures == BREAKER_FAILURES:
                    log.warning("redis unavailable (%s: %s); skipping it for %.0fs at a time",
                                type(exc).__name__, exc, BREAKER_COOLDOWN_SECONDS)
                self._open_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
            return default
        if self._failures >= BREAKER_FAILURES:
            log.info("redis available again")
        self._failures = 0
        return result

    async def get(self, key: str) -> bytes | None:
        if self.redis is not None:
            redis = self.redis
            return await self._redis(lambda: redis.get(key), None)
        hit = self._mem.get(key)
        if hit is None or hit[0] < time.monotonic():
            self._mem.pop(key, None)
            return None
        return hit[1]

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        if self.redis is not None:
            redis = self.redis
            await self._redis(lambda: redis.set(key, value, ex=ttl), None)
            return
        if len(self._mem) > 5000:  # crude bound for dev
            now = time.monotonic()
            self._mem = {k: v for k, v in self._mem.items() if v[0] >= now}
        self._mem[key] = (time.monotonic() + ttl, value)

    async def incr_window(self, key: str, window: int) -> int:
        """Fixed-window counter; returns the count including this hit."""
        bucket = f"{key}:{int(time.time()) // window}"
        if self.redis is not None:
            pipe = self.redis.pipeline()
            pipe.incr(bucket)
            pipe.expire(bucket, window + 1)
            res = await self._redis(pipe.execute, None)
            return 0 if res is None else int(res[0])  # fail open
        raw = await self.get(bucket)
        count = int(raw or 0) + 1
        await self.set(bucket, str(count).encode(), window + 1)
        return count

    async def ping(self) -> bool:
        if self.redis is None:
            return True
        redis = self.redis
        return bool(await self._redis(redis.ping, False))

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
