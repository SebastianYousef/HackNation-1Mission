"""Shared key/value store: Redis when REDIS_URL is set, else an in-process TTL dict (dev only).

Used for the GET response cache, AI rate-limit counters and job status. With
Redis every replica sees the same state, which is what keeps replicas stateless.
"""
from __future__ import annotations

import time

from redis.asyncio import Redis


class Store:
    def __init__(self, redis: Redis | None):
        self.redis = redis
        self._mem: dict[str, tuple[float, bytes]] = {}

    @classmethod
    def from_url(cls, url: str | None) -> "Store":
        return cls(Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2) if url else None)

    async def get(self, key: str) -> bytes | None:
        if self.redis is not None:
            try:
                return await self.redis.get(key)
            except Exception:  # cache must never break a request
                return None
        hit = self._mem.get(key)
        if hit is None or hit[0] < time.monotonic():
            self._mem.pop(key, None)
            return None
        return hit[1]

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        if self.redis is not None:
            try:
                await self.redis.set(key, value, ex=ttl)
            except Exception:
                pass
            return
        if len(self._mem) > 5000:  # crude bound for dev
            now = time.monotonic()
            self._mem = {k: v for k, v in self._mem.items() if v[0] >= now}
        self._mem[key] = (time.monotonic() + ttl, value)

    async def incr_window(self, key: str, window: int) -> int:
        """Fixed-window counter; returns the count including this hit."""
        bucket = f"{key}:{int(time.time()) // window}"
        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.incr(bucket)
                pipe.expire(bucket, window + 1)
                count, _ = await pipe.execute()
                return int(count)
            except Exception:
                return 0  # fail open
        raw = await self.get(bucket)
        count = int(raw or 0) + 1
        await self.set(bucket, str(count).encode(), window + 1)
        return count

    async def ping(self) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
