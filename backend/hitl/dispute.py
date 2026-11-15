"""Developer disputes: the reversibility path.

A posted finding can be wrong. The author replies ``/dispute F-1a2b3c4d why``
on the pull request; the finding is marked disputed, the dispute is recorded
as feedback, and a maintainer gets it in the queue to uphold or retract.
"""

from __future__ import annotations

import re
from typing import Any

from backend.models.enums import EventType, FindingStatus, HitlKind, Severity, Verdict
from backend.observability.events import EventEmitter

_DISPUTE = re.compile(r"(?mi)^\s*/dispute\s+(F-[0-9a-f]{8})\b[ \t]*(.*)$")
_DISPUTABLE = {FindingStatus.POSTED.value, FindingStatus.UPHELD.value}


def parse_disputes(comment_body: str) -> list[tuple[str, str]]:
    """``/dispute F-xxxxxxxx reason`` lines in a comment -> [(finding_id, reason)]."""
    return [(m.group(1), m.group(2).strip()[:500]) for m in _DISPUTE.finditer(comment_body or "")]


class DisputeHandler:
    def __init__(self, store: Any, emitter: EventEmitter, github: Any) -> None:
        self._store = store
        self._emitter = emitter
        self._github = github

    async def handle(
        self, *, repo: str, pr_number: int, finding_id: str, reason: str, actor: str, installation_id: int | None
    ) -> dict[str, Any]:
        finding = await self._store.find_finding(repo, pr_number, finding_id)
        if finding is None:
            await self._reply(repo, pr_number, f"I could not find a finding `{finding_id}` on this pull request.", installation_id)
            return {"status": "unknown_finding", "finding_id": finding_id}
        if finding["status"] not in _DISPUTABLE:
            return {"status": "not_disputable", "finding_id": finding_id, "finding_status": finding["status"]}

        review_id = finding["review_id"]
        await self._store.set_finding_status(review_id, [finding_id], FindingStatus.DISPUTED.value)
        await self._store.add_feedback(
            review_id=review_id, finding_id=finding_id, repo=repo, agent_type=finding["agent_type"],
            category=finding["category"], verdict=Verdict.DISPUTED.value, actor=actor, source="dispute", note=reason,
        )  # fmt: skip
        priority = Severity(finding["severity"]).rank * 100
        hitl_id, created = await self._store.enqueue_hitl(
            review_id, HitlKind.DISPUTE.value, reason or "disputed without a reason", priority,
            finding_id=finding_id, requested_by=actor,
        )  # fmt: skip
        await self._emitter.emit(
            EventType.FEEDBACK, agent=finding["agent_type"], name=finding["category"], review_id=review_id,
            outcome=Verdict.DISPUTED.value, confidence=finding["confidence"], finding_id=finding_id,
            actor=actor, source="dispute", reason=reason, hitl_id=hitl_id,
        )  # fmt: skip
        if created:
            await self._reply(
                repo, pr_number,
                f"Recorded your dispute of `{finding_id}`. A maintainer will review it, and the outcome is fed back "
                "into how confident the reviewer is about findings like this one.",
                installation_id,
            )  # fmt: skip
        return {"status": "disputed", "finding_id": finding_id, "hitl_id": hitl_id}

    async def _reply(self, repo: str, pr_number: int, body: str, installation_id: int | None) -> None:
        try:
            await self._github.create_issue_comment(repo, pr_number, body, installation_id)
        except Exception:  # noqa: BLE001, S110 - the dispute is recorded; the acknowledgement is best effort
            pass
