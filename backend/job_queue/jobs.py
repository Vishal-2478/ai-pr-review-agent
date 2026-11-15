"""Job bodies, independent of the queue that runs them."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.integrations.github_client import GitHubError
from backend.models.enums import EventType, ReviewStatus
from backend.models.schemas import PRContext

if TYPE_CHECKING:
    from backend.container import Services

log = logging.getLogger(__name__)

_RERUNNABLE = {ReviewStatus.FAILED.value, ReviewStatus.SKIPPED.value, ReviewStatus.SUPERSEDED.value}


async def review_pull_request(services: Services, job: dict[str, Any]) -> dict[str, Any]:
    """Review one commit of one pull request.

    Safe to run any number of times for the same commit: the review row is
    unique per (repo, PR, head SHA), a finished review is never redone, and an
    interrupted one resumes from its last checkpoint.
    """
    s = services
    pr = PRContext(**job["pr"])
    force = bool(job.get("force"))
    review_id, created = await s.store.create_review(pr)
    review = await s.store.get_review(review_id) or {}

    if not created and ReviewStatus(review["status"]).terminal:
        if not (force and review["status"] in _RERUNNABLE):
            await s.emitter.emit(
                EventType.DECISION, agent="orchestrator", name="duplicate_review_skipped", review_id=review_id,
                outcome="skipped", status=review["status"], delivery_id=pr.delivery_id,
            )  # fmt: skip
            return {"review_id": review_id, "status": review["status"], "skipped": "already reviewed this commit"}
        await s.engine.forget(review_id)
        await s.store.update_review(review_id, status=ReviewStatus.QUEUED.value, error=None)

    if not job.get("files"):
        # Only the newest commit is worth reviewing: a push that arrived while
        # this job waited in the queue supersedes it.
        try:
            current = await s.github.get_pull(pr.repo, pr.pr_number, pr.installation_id)
        except GitHubError as exc:
            if exc.status == 404:
                await s.store.update_review(review_id, completed=True, status=ReviewStatus.SKIPPED.value, reasons=["pull request not found"])
                return {"review_id": review_id, "status": ReviewStatus.SKIPPED.value}
            raise
        if current.get("state") == "closed":
            await s.store.update_review(review_id, completed=True, status=ReviewStatus.SKIPPED.value, reasons=["pull request is closed"])
            return {"review_id": review_id, "status": ReviewStatus.SKIPPED.value}
        if current["head"]["sha"] != pr.head_sha:
            await s.store.update_review(
                review_id, completed=True, status=ReviewStatus.SUPERSEDED.value,
                reasons=[f"superseded by {current['head']['sha'][:7]}"],
            )  # fmt: skip
            return {"review_id": review_id, "status": ReviewStatus.SUPERSEDED.value}

    payload: dict[str, Any] = {"review_id": review_id, "pr": pr.model_dump()}
    if job.get("files"):
        payload["files"] = job["files"]
    async with s.emitter.span(
        "review", agent="orchestrator", review_id=review_id, repo=pr.repo, pr_number=pr.pr_number,
        head_sha=pr.head_sha, delivery_id=pr.delivery_id, engine=s.engine.name,
    ) as span:  # fmt: skip
        final = await s.engine.run_or_resume(review_id, payload)
        span.set(final_status=final.get("final_status"))
    return {"review_id": review_id, "status": final.get("final_status")}


async def mark_review_failed(services: Services, job: dict[str, Any], error: str) -> None:
    """Dead-letter handling: the review is visibly failed, never silently lost."""
    s = services
    try:
        pr = PRContext(**job["pr"])
        review_id, _ = await s.store.create_review(pr)
        review = await s.store.get_review(review_id) or {}
        if not ReviewStatus(review.get("status", "queued")).terminal:
            await s.store.update_review(review_id, completed=True, status=ReviewStatus.FAILED.value, error=error[:1000])
        await s.emitter.emit(
            EventType.ERROR, agent="orchestrator", name="review_dead_lettered", review_id=review_id,
            outcome="failed", error=error[:500],
        )  # fmt: skip
        await s.alerter.notify(
            "Review failed after all retries", f"{pr.repo}#{pr.pr_number} at {pr.head_sha[:7]}: {error[:300]}",
            severity="error", review_id=review_id,
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("could not record failed review")


async def publish_review(
    services: Services, review_id: str, finding_ids: list[str], actor: str = "", publish_withheld: bool = False
) -> dict[str, Any]:
    return await services.publisher.publish(review_id, finding_ids, actor=actor, publish_withheld=publish_withheld)


async def index_repository(
    services: Services, repo: str, ref: str = "HEAD", installation_id: int | None = None,
    only_paths: list[str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    report = await services.indexer.index_github(
        repo, services.github, ref, installation_id, only_paths=set(only_paths) if only_paths else None
    )
    return {k: v for k, v in report.__dict__.items() if k != "errors"} | {"errors": report.errors[:10]}


async def handle_dispute(services: Services, payload: dict[str, Any]) -> dict[str, Any]:
    return await services.disputes.handle(**payload)


async def check_queue_health(services: Services) -> list[str]:
    return await services.monitor.check()
