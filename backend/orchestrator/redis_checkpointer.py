"""LangGraph checkpoint saver on plain Redis commands.

Why not ``langgraph-checkpoint-redis``? It needs the RedisJSON and RediSearch
modules (Redis 8+ or Redis Stack). The design checkpoints to "the same Redis
we already run for the queue", and that Redis is often a managed plain
instance (Upstash's free tier, for one) with no modules. This saver uses only
hashes, sets and EXPIRE, so it runs anywhere ARQ does (ADR-003).

Layout (every key carries a TTL; checkpoints are scratch state, not truth):

    {p}:cp:{thread}:{ns}          HASH  checkpoint_id      -> checkpoint, metadata, parent id
    {p}:blob:{thread}:{ns}        HASH  channel\\0version   -> serialised channel value
    {p}:wr:{thread}:{ns}:{cp_id}  HASH  task_id\\0index     -> pending write
    {p}:keys:{thread}             SET   every key above, for thread deletion

Pending writes are what make resume cheap: when three of four specialists
finished before a crash, their results are stored against the checkpoint and
only the fourth runs again.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

import ormsgpack
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)

SEP = "\x00"


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


class PlainRedisSaver(BaseCheckpointSaver[str]):
    def __init__(self, redis: Any, *, prefix: str = "lg", ttl_s: int = 7 * 24 * 3600) -> None:
        super().__init__()
        self._redis = redis  # redis.asyncio client created with decode_responses=False
        self._prefix = prefix
        self._ttl = ttl_s

    # ------------------------------------------------------------------- keys
    def _cp(self, thread: str, ns: str) -> str:
        return f"{self._prefix}:cp:{thread}:{ns}"

    def _blob(self, thread: str, ns: str) -> str:
        return f"{self._prefix}:blob:{thread}:{ns}"

    def _wr(self, thread: str, ns: str, checkpoint_id: str) -> str:
        return f"{self._prefix}:wr:{thread}:{ns}:{checkpoint_id}"

    def _keys(self, thread: str) -> str:
        return f"{self._prefix}:keys:{thread}"

    # ------------------------------------------------------------------ write
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        body = checkpoint.copy()
        values: dict[str, Any] = body.pop("channel_values")  # type: ignore[misc]
        cp_key, blob_key, idx_key = self._cp(thread, ns), self._blob(thread, ns), self._keys(thread)

        pipe = self._redis.pipeline(transaction=True)
        for channel, version in new_versions.items():
            typed = self.serde.dumps_typed(values[channel]) if channel in values else ("empty", b"")
            pipe.hset(blob_key, f"{channel}{SEP}{version}", ormsgpack.packb([typed[0], typed[1]]))
        cp_type, cp_bytes = self.serde.dumps_typed(body)
        md_type, md_bytes = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        parent = config["configurable"].get("checkpoint_id")
        pipe.hset(cp_key, checkpoint["id"], ormsgpack.packb([cp_type, cp_bytes, md_type, md_bytes, parent]))
        pipe.sadd(idx_key, cp_key, blob_key)
        for key in (cp_key, blob_key, idx_key):
            pipe.expire(key, self._ttl)
        await pipe.execute()
        return {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        key = self._wr(thread, ns, config["configurable"]["checkpoint_id"])
        pipe = self._redis.pipeline(transaction=True)
        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            typed = self.serde.dumps_typed(value)
            packed = ormsgpack.packb([task_id, channel, typed[0], typed[1], task_path])
            field = f"{task_id}{SEP}{write_idx}"
            if write_idx >= 0:
                pipe.hsetnx(key, field, packed)  # a task's regular writes are recorded once
            else:
                pipe.hset(key, field, packed)  # special writes (errors, interrupts) replace
        pipe.sadd(self._keys(thread), key)
        pipe.expire(key, self._ttl)
        pipe.expire(self._keys(thread), self._ttl)
        await pipe.execute()

    # ------------------------------------------------------------------- read
    async def _load(self, thread: str, ns: str, checkpoint_id: str, raw: bytes, config: RunnableConfig | None) -> CheckpointTuple:
        cp_type, cp_bytes, md_type, md_bytes, parent = ormsgpack.unpackb(raw)
        checkpoint: Checkpoint = self.serde.loads_typed((cp_type, cp_bytes))
        versions: dict[str, Any] = checkpoint["channel_versions"]
        channel_values: dict[str, Any] = {}
        if versions:
            fields = [f"{channel}{SEP}{version}" for channel, version in versions.items()]
            blobs = await self._redis.hmget(self._blob(thread, ns), fields)
            for channel, blob in zip(versions, blobs, strict=True):
                if blob is None:
                    continue
                b_type, b_bytes = ormsgpack.unpackb(blob)
                if b_type != "empty":
                    channel_values[channel] = self.serde.loads_typed((b_type, b_bytes))
        stored = await self._redis.hgetall(self._wr(thread, ns, checkpoint_id))
        writes = []
        for field, packed in stored.items():
            task_id, channel, w_type, w_bytes, task_path = ormsgpack.unpackb(packed)
            idx = int(_s(field).rsplit(SEP, 1)[1])
            writes.append((writes_sort_key(task_path, task_id, idx), task_id, channel, (w_type, w_bytes)))
        writes.sort(key=lambda w: w[0])
        return CheckpointTuple(
            config=config or {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}},
            checkpoint={**checkpoint, "channel_values": channel_values},
            metadata=self.serde.loads_typed((md_type, md_bytes)),
            parent_config=(
                {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": parent}} if parent else None
            ),
            pending_writes=[(task_id, channel, self.serde.loads_typed(typed)) for _, task_id, channel, typed in writes],
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cp_key = self._cp(thread, ns)
        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id:
            raw = await self._redis.hget(cp_key, checkpoint_id)
            return await self._load(thread, ns, checkpoint_id, raw, config) if raw else None
        ids = [_s(k) for k in await self._redis.hkeys(cp_key)]
        if not ids:
            return None
        latest = max(ids)  # checkpoint ids are time-ordered, so the greatest is the newest
        raw = await self._redis.hget(cp_key, latest)
        return await self._load(thread, ns, latest, raw, None) if raw else None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        stored = await self._redis.hgetall(self._cp(thread, ns))
        before_id = get_checkpoint_id(before) if before else None
        yielded = 0
        for checkpoint_id in sorted((_s(k) for k in stored), reverse=True):
            if before_id and checkpoint_id >= before_id:
                continue
            raw = stored.get(checkpoint_id.encode()) or stored.get(checkpoint_id)
            item = await self._load(thread, ns, checkpoint_id, raw, None)
            if filter and not all(item.metadata.get(k) == v for k, v in filter.items()):
                continue
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    async def adelete_thread(self, thread_id: str) -> None:
        idx_key = self._keys(thread_id)
        keys = [_s(k) for k in await self._redis.smembers(idx_key)]
        if keys:
            await self._redis.delete(*keys)
        await self._redis.delete(idx_key)

    def get_next_version(self, current: str | None, channel: None) -> str:
        current_v = 0 if current is None else int(str(current).split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"  # noqa: S311 - tie-breaker, not crypto

    # The graph is only ever driven through the async API.
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("PlainRedisSaver is async-only; use ainvoke/astream")

    def put(self, *args: Any, **kwargs: Any) -> RunnableConfig:
        raise NotImplementedError("PlainRedisSaver is async-only; use ainvoke/astream")

    def put_writes(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("PlainRedisSaver is async-only; use ainvoke/astream")
