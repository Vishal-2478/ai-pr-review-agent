"""Job queue port.

The webhook handler has about ten seconds to answer GitHub and a review takes
far longer, so ingress and work are decoupled by a queue. Production uses ARQ
on Redis. ``InlineJobQueue`` runs the same job functions in-process for
development without Redis, for the offline demo and for tests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

DLQ_KEY = "pr-review:dlq"
ARQ_QUEUE_KEY = "arq:queue"


class JobQueue(Protocol):
    async def enqueue(self, name: str, *args: Any, job_id: str | None = None) -> bool:
        """Queue a job. Returns False when a job with this id is already queued or running."""

    async def stats(self) -> dict[str, Any]: ...


class ArqJobQueue:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def enqueue(self, name: str, *args: Any, job_id: str | None = None) -> bool:
        # ARQ refuses a second job with the same id while the first is queued,
        # running or has a kept result: a third layer of duplicate protection.
        job = await self._pool.enqueue_job(name, *args, _job_id=job_id)
        return job is not None

    async def stats(self) -> dict[str, Any]:
        return {
            "backend": "arq",
            "queued": int(await self._pool.zcard(ARQ_QUEUE_KEY)),
            "dead_letter": int(await self._pool.llen(DLQ_KEY)),
        }


Handler = Callable[..., Awaitable[Any]]


class InlineJobQueue:
    """Runs jobs in this process. ``background=True`` returns immediately, like a real queue."""

    def __init__(self, background: bool = False) -> None:
        self._handlers: dict[str, Handler] = {}
        self._background = background
        self._active: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.failed: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []

    def register(self, name: str, handler: Handler) -> None:
        self._handlers[name] = handler

    async def _run(self, name: str, args: tuple[Any, ...], job_id: str | None) -> None:
        try:
            result = await self._handlers[name](*args)
            self.completed.append({"job": name, "job_id": job_id, "result": result})
        except Exception as exc:  # noqa: BLE001 - mirror a worker: a failed job is recorded, not raised at the caller
            log.exception("inline job %s failed", name)
            self.failed.append({"job": name, "job_id": job_id, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if job_id:
                self._active.discard(job_id)

    async def enqueue(self, name: str, *args: Any, job_id: str | None = None) -> bool:
        if name not in self._handlers:
            raise KeyError(f"no handler registered for job '{name}'")
        if job_id:
            if job_id in self._active:
                return False
            self._active.add(job_id)
        if self._background:
            task = asyncio.create_task(self._run(name, args, job_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await self._run(name, args, job_id)
        return True

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stats(self) -> dict[str, Any]:
        return {"backend": "inline", "queued": len(self._tasks), "dead_letter": len(self.failed)}
