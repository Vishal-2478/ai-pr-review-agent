"""Redis connections.

Redis has two jobs here and neither is a database of record: it is the job
queue (ARQ) and the workflow checkpoint store. Anything that must survive
Redis being wiped lives in Postgres.
"""

from __future__ import annotations

from typing import Any


def create_redis(url: str, *, decode_responses: bool = False) -> Any:
    import redis.asyncio as redis

    return redis.from_url(url, decode_responses=decode_responses, socket_timeout=10, socket_connect_timeout=5)


def arq_settings(url: str) -> Any:
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(url)
