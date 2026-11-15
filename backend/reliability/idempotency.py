"""Idempotency keys.

GitHub redelivers a webhook when it does not get a fast 2xx, and people click
"Redeliver". ``claim`` answers one question atomically: is this the first time
we have seen this key?
"""

from __future__ import annotations

import time
from typing import Any, Protocol


class IdempotencyStore(Protocol):
    async def claim(self, key: str, ttl_s: int) -> bool:
        """Return True exactly once per key within ``ttl_s``."""

    async def release(self, key: str) -> None:
        """Forget a key, so a delivery we failed to enqueue can be retried."""


class RedisIdempotencyStore:
    def __init__(self, redis: Any, prefix: str = "idem") -> None:
        self._redis = redis
        self._prefix = prefix

    async def claim(self, key: str, ttl_s: int) -> bool:
        # SET NX EX is a single atomic command: two racing deliveries cannot both win.
        return bool(await self._redis.set(f"{self._prefix}:{key}", "1", nx=True, ex=ttl_s))

    async def release(self, key: str) -> None:
        await self._redis.delete(f"{self._prefix}:{key}")


class InMemoryIdempotencyStore:
    """Single-process fallback for development and tests. Not shared between replicas."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    async def claim(self, key: str, ttl_s: int) -> bool:
        now = time.monotonic()
        if len(self._seen) > 50_000:
            self._seen = {k: exp for k, exp in self._seen.items() if exp > now}
        expiry = self._seen.get(key)
        if expiry is not None and expiry > now:
            return False
        self._seen[key] = now + ttl_s
        return True

    async def release(self, key: str) -> None:
        self._seen.pop(key, None)
