"""ARQ worker: the "chef" behind the webhook "receptionist".

Run with:  arq backend.job_queue.worker.WorkerSettings

Retry policy: transient failures are retried with exponential backoff up to
JOB_MAX_TRIES; after that the job is dead-lettered (recorded on the review,
pushed to a Redis list, and alerted on) instead of being dropped. A worker
that is killed mid-review does not lose the job either: ARQ re-queues it and
the workflow resumes from its last Redis checkpoint.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arq import Retry, cron

from backend.config import get_settings
from backend.container import build_services
from backend.job_queue import jobs
from backend.job_queue.enqueue import DLQ_KEY
from backend.memory.redis_client import arq_settings
from backend.observability.logging import configure_logging
from backend.reliability.retry import is_retryable

log = logging.getLogger(__name__)
_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.log_level)
    ctx["services"] = await build_services(_settings, role="worker", arq_pool=ctx["redis"])
    log.info("worker ready: engine=%s provider=%s", ctx["services"].engine.name, _settings.resolved_llm_provider)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["services"].close()


async def review_pull_request(ctx: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    services = ctx["services"]
    attempt = int(ctx.get("job_try", 1))
    try:
        return await jobs.review_pull_request(services, job)
    except Exception as exc:
        if is_retryable(exc) and attempt < _settings.job_max_tries:
            delay = min(300, 15 * 2 ** (attempt - 1))
            log.warning("review job attempt %s failed (%s); retrying in %ss", attempt, exc, delay)
            raise Retry(defer=delay) from exc
        error = f"{type(exc).__name__}: {exc}"
        await jobs.mark_review_failed(services, job, error)
        await ctx["redis"].lpush(DLQ_KEY, json.dumps({"job": job, "error": error[:500], "attempts": attempt}))
        await ctx["redis"].ltrim(DLQ_KEY, 0, 499)
        raise


async def publish_review(
    ctx: dict[str, Any], review_id: str, finding_ids: list[str], actor: str = "", publish_withheld: bool = False
) -> dict[str, Any]:
    attempt = int(ctx.get("job_try", 1))
    try:
        return await jobs.publish_review(ctx["services"], review_id, finding_ids, actor, publish_withheld)
    except Exception as exc:
        if is_retryable(exc) and attempt < _settings.job_max_tries:
            raise Retry(defer=min(300, 15 * 2 ** (attempt - 1))) from exc
        raise


async def index_repository(
    ctx: dict[str, Any], repo: str, ref: str = "HEAD", installation_id: int | None = None,
    only_paths: list[str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    return await jobs.index_repository(ctx["services"], repo, ref, installation_id, only_paths)


async def handle_dispute(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return await jobs.handle_dispute(ctx["services"], payload)


async def check_queue_health(ctx: dict[str, Any]) -> list[str]:
    return await jobs.check_queue_health(ctx["services"])


class WorkerSettings:
    functions = [review_pull_request, publish_review, index_repository, handle_dispute]
    cron_jobs = [cron(check_queue_health, minute={0, 10, 20, 30, 40, 50}, run_at_startup=False)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = arq_settings(_settings.redis_url) if _settings.redis_url else None
    max_jobs = 4
    job_timeout = int(_settings.agent_timeout_s * 3 + 120)
    max_tries = _settings.job_max_tries
    keep_result = 3600
    retry_jobs = True
