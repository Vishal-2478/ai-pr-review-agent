"""Publish a review to GitHub, exactly once.

Both paths end here: the confident review the gate auto-posts, and the one a
human approved from the queue. Posting is the one irreversible, public thing
the system does, so this is where the last safeguards live:

- **dedup before posting**: if GitHub already has a review carrying this
  review's marker (a retried job, a crash after the POST), nothing is posted;
- **private first**: critical security findings are withheld from the public
  thread unless a human explicitly chooses to publish them;
- **graceful fallback**: if GitHub rejects the inline comments (the diff moved),
  the same findings are posted in the review body instead of being lost.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.config import Settings
from backend.integrations.github_client import GitHubClient, GitHubError
from backend.integrations.review_formatter import format_review, marker
from backend.models.enums import EventType, FindingStatus, ReviewStatus
from backend.observability.events import EventEmitter

log = logging.getLogger(__name__)

_PUBLISHABLE = {FindingStatus.PROPOSED.value, FindingStatus.PENDING_HUMAN.value}


class ReviewPublisher:
    def __init__(self, store: Any, github: GitHubClient, emitter: EventEmitter, settings: Settings) -> None:
        self._store = store
        self._github = github
        self._emitter = emitter
        self._settings = settings

    async def publish(
        self,
        review_id: str,
        finding_ids: list[str] | None = None,
        *,
        actor: str = "",
        publish_withheld: bool = False,
    ) -> dict[str, Any]:
        review = await self._store.get_review(review_id)
        if review is None:
            raise ValueError(f"unknown review {review_id}")
        rows = await self._store.get_findings(review_id)
        suppressed = sum(1 for r in rows if r["status"] == FindingStatus.SUPPRESSED.value)
        selected = [
            r for r in rows
            if (finding_ids is None and r["status"] in _PUBLISHABLE) or (finding_ids is not None and r["finding_id"] in finding_ids)
        ]  # fmt: skip

        withheld: list[dict[str, Any]] = []
        if self._settings.withhold_critical_security_from_public and not publish_withheld:
            withheld = [r for r in selected if r["severity"] == "critical" and r["agent_type"] == "security"]
            selected = [r for r in selected if r not in withheld]
        if withheld:
            await self._store.set_finding_status(review_id, [r["finding_id"] for r in withheld], FindingStatus.WITHHELD.value)

        repo, number, installation = review["repo"], review["pr_number"], review.get("installation_id")
        async with self._emitter.span("publish_review", agent="publisher", review_id=review_id) as span:
            if not selected and not (self._settings.post_clean_reviews and not withheld):
                # Nothing public to say. Posting "a problem was reported privately" would itself be a hint.
                await self._store.update_review(review_id, completed=True, status=ReviewStatus.RESOLVED.value)
                span.set(posted=0, withheld=len(withheld))
                return {"posted": 0, "withheld": len(withheld), "github_review_id": None, "deduplicated": False}

            existing = await self._find_existing(repo, number, installation, review_id)
            if existing is not None:
                github_id, deduplicated = existing, True
            else:
                github_id, deduplicated = await self._post(review, selected, suppressed, actor), False

            await self._store.set_finding_status(review_id, [r["finding_id"] for r in selected], FindingStatus.POSTED.value)
            await self._store.update_review(
                review_id, completed=True, status=ReviewStatus.POSTED.value, github_review_id=github_id
            )
            await self._emitter.emit(
                EventType.DECISION, agent="publisher", name="review_posted", review_id=review_id,
                outcome="deduplicated" if deduplicated else "posted", github_review_id=github_id,
                findings=[r["finding_id"] for r in selected], withheld=[r["finding_id"] for r in withheld],
                actor=actor or "agent",
            )  # fmt: skip
            span.set(posted=len(selected), withheld=len(withheld), deduplicated=deduplicated)
            return {
                "posted": len(selected), "withheld": len(withheld), "github_review_id": github_id,
                "deduplicated": deduplicated,
            }  # fmt: skip

    async def _find_existing(self, repo: str, number: int, installation: int | None, review_id: str) -> int | None:
        tag = marker(review_id)
        for existing in await self._github.list_reviews(repo, number, installation):
            if tag in (existing.get("body") or ""):
                log.info("review %s already on GitHub as %s; not posting again", review_id, existing.get("id"))
                return existing.get("id")
        return None

    async def _post(self, review: dict[str, Any], findings: list[dict[str, Any]], suppressed: int, actor: str) -> int | None:
        kwargs = {
            "overall_confidence": review.get("overall_confidence"), "coverage": review.get("coverage"),
            "max_inline": self._settings.max_inline_comments, "suppressed": suppressed, "approved_by": actor,
        }  # fmt: skip
        repo, number, installation = review["repo"], review["pr_number"], review.get("installation_id")
        body, comments = format_review(review["id"], review["head_sha"], findings, **kwargs)
        try:
            created = await self._github.create_review(
                repo, number, commit_id=review["head_sha"], body=body, comments=comments, installation_id=installation
            )
        except GitHubError as exc:
            if exc.status != 422 or not comments:
                raise
            # The lines no longer exist in the diff (force-push, rebase). Keep the findings, drop the anchors.
            log.warning("GitHub rejected inline comments for %s#%s; posting in the review body", repo, number)
            body, _ = format_review(review["id"], review["head_sha"], findings, inline=False, **kwargs)
            created = await self._github.create_review(
                repo, number, commit_id=review["head_sha"], body=body, comments=[], installation_id=installation
            )
        return created.get("id")
