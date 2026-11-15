"""The human approval queue.

When the gate will not let the agent act alone, the review waits here. The
wait state lives in Postgres (``hitl_reviews``), not in a paused workflow: a
human may take days, and an approval must survive a Redis flush or a deploy
(ADR-005).

A human decides finding by finding. Accepted findings are published; every
verdict, accepted or rejected, is recorded as feedback for calibration.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from backend.agents.contracts import GateOutcome
from backend.config import Settings
from backend.models.enums import (
    EventType,
    FindingStatus,
    GateDecision,
    HitlKind,
    HitlStatus,
    ReviewStatus,
    Verdict,
)
from backend.models.schemas import HitlDecisionRequest, PRContext
from backend.observability.alerting import Alerter
from backend.observability.audit import audit
from backend.observability.events import EventEmitter

log = logging.getLogger(__name__)

PublishFn = Callable[[str, list[str], str, bool], Awaitable[Any]]
CommentFn = Callable[[str, int, str, int | None], Awaitable[Any]]


class DecisionConflict(Exception):
    """The item was already resolved by someone else."""


class ApprovalQueue:
    def __init__(
        self,
        store: Any,
        emitter: EventEmitter,
        alerter: Alerter,
        settings: Settings,
        publish: PublishFn,
        comment: CommentFn | None = None,
    ) -> None:
        self._store = store
        self._emitter = emitter
        self._alerter = alerter
        self._settings = settings
        self._publish = publish
        self._comment = comment

    # ----------------------------------------------------------------- submit
    async def submit(self, review_id: str, pr: PRContext, outcome: GateOutcome, critical_summaries: list[str]) -> str:
        escalated = outcome.decision is GateDecision.ESCALATE
        kind = HitlKind.ESCALATION if escalated else HitlKind.APPROVAL
        hitl_id, created = await self._store.enqueue_hitl(
            review_id, kind.value, "; ".join(outcome.reasons)[:500], outcome.priority
        )
        if not created:
            return hitl_id  # a retried job: already waiting, do not page twice
        await self._emitter.emit(
            EventType.ESCALATION, agent="aggregator", name=kind.value, review_id=review_id,
            outcome="queued", confidence=outcome.overall_confidence, hitl_id=hitl_id,
            priority=outcome.priority, findings=outcome.to_review, reasons=outcome.reasons,
        )  # fmt: skip
        if escalated:
            # Critical findings are announced privately. Nothing is posted on the pull request.
            await self._alerter.notify(
                "Critical finding needs a human decision",
                f"{pr.repo}#{pr.pr_number} \"{pr.title}\" by {pr.author or 'unknown'}\n"
                + "\n".join(f"- {s}" for s in critical_summaries[:5]),
                severity="critical", review_id=review_id, hitl_id=hitl_id, pull_request=pr.html_url,
            )  # fmt: skip
        return hitl_id

    # ----------------------------------------------------------------- decide
    async def decide(self, hitl_id: str, request: HitlDecisionRequest, reviewer: str) -> dict[str, Any]:
        item = await self._store.get_hitl(hitl_id)
        if item is None:
            raise KeyError(hitl_id)
        if item["status"] != HitlStatus.PENDING.value:
            raise DecisionConflict(f"already {item['status']} by {item.get('reviewer') or 'someone'}")
        if request.action not in ("approve", "reject"):
            raise ValueError("action must be 'approve' or 'reject'")
        if item["kind"] == HitlKind.DISPUTE.value:
            return await self._decide_dispute(item, request, reviewer)

        review_id = item["review_id"]
        pending = [f for f in await self._store.get_findings(review_id) if f["status"] == FindingStatus.PENDING_HUMAN.value]
        overrides = {d.finding_id: d for d in request.findings}
        accepted, rejected = [], []
        for f in pending:
            verdict = overrides[f["finding_id"]].verdict if f["finding_id"] in overrides else None
            if request.action == "reject" or verdict == Verdict.REJECTED.value:
                rejected.append(f)
            else:
                accepted.append(f)

        status = HitlStatus.APPROVED if accepted else HitlStatus.REJECTED
        # Compare-and-set: of two reviewers deciding at once, exactly one proceeds.
        if not await self._store.resolve_hitl(hitl_id, status.value, reviewer, request.note):
            raise DecisionConflict("already resolved by someone else")

        for f, verdict in [(f, Verdict.ACCEPTED) for f in accepted] + [(f, Verdict.REJECTED) for f in rejected]:
            note = overrides[f["finding_id"]].note if f["finding_id"] in overrides else request.note
            await self._record_feedback(f, verdict, reviewer, "hitl", note)
        await self._store.set_finding_status(review_id, [f["finding_id"] for f in rejected], FindingStatus.REJECTED.value)
        await audit(
            self._emitter, actor=reviewer, action=f"hitl_{status.value}", review_id=review_id, hitl_id=hitl_id,
            accepted=[f["finding_id"] for f in accepted], rejected=[f["finding_id"] for f in rejected], note=request.note,
        )  # fmt: skip

        if accepted:
            await self._publish(review_id, [f["finding_id"] for f in accepted], reviewer, request.publish_withheld)
        else:
            await self._store.update_review(review_id, completed=True, status=ReviewStatus.REJECTED.value)
        return {
            "hitl_id": hitl_id, "status": status.value, "accepted": len(accepted), "rejected": len(rejected),
            "publishing": bool(accepted),
        }  # fmt: skip

    async def _decide_dispute(self, item: dict[str, Any], request: HitlDecisionRequest, reviewer: str) -> dict[str, Any]:
        """approve = the finding stands (upheld); reject = the developer was right (retracted)."""
        upheld = request.action == "approve"
        status = HitlStatus.APPROVED if upheld else HitlStatus.REJECTED
        if not await self._store.resolve_hitl(item["id"], status.value, reviewer, request.note):
            raise DecisionConflict("already resolved by someone else")
        review_id, finding_id = item["review_id"], item["finding_id"]
        finding = next((f for f in await self._store.get_findings(review_id) if f["finding_id"] == finding_id), None)
        new_status = FindingStatus.UPHELD if upheld else FindingStatus.RETRACTED
        await self._store.set_finding_status(review_id, [finding_id], new_status.value)
        if finding:
            await self._record_feedback(
                finding, Verdict.ACCEPTED if upheld else Verdict.REJECTED, reviewer, "dispute-resolution", request.note
            )
        await audit(
            self._emitter, actor=reviewer, action=f"dispute_{new_status.value}", review_id=review_id,
            hitl_id=item["id"], finding_id=finding_id, note=request.note,
        )  # fmt: skip
        if self._comment is not None:
            verdict = "stands" if upheld else "has been retracted. Thank you for the correction"
            note = f"\n\n> {request.note}" if request.note else ""
            try:
                await self._comment(
                    item["repo"], item["pr_number"],
                    f"Dispute of `{finding_id}` reviewed by {reviewer or 'a maintainer'}: the finding {verdict}.{note}",
                    item.get("installation_id"),
                )  # fmt: skip
            except Exception as exc:  # noqa: BLE001 - the decision is recorded; the courtesy reply is best effort
                log.warning("could not post dispute resolution comment: %s", exc)
        return {"hitl_id": item["id"], "status": status.value, "finding": finding_id, "outcome": new_status.value}

    async def _record_feedback(self, finding: dict[str, Any], verdict: Verdict, actor: str, source: str, note: str = "") -> None:
        await self._store.add_feedback(
            review_id=finding["review_id"], finding_id=finding["finding_id"], repo=finding["repo"],
            agent_type=finding["agent_type"], category=finding["category"], verdict=verdict.value,
            actor=actor, source=source, note=note or "",
        )  # fmt: skip
        # Mirrored onto the event spine so rejection rate per agent shows up in the rollups.
        await self._emitter.emit(
            EventType.FEEDBACK, agent=finding["agent_type"], name=finding["category"],
            review_id=finding["review_id"], outcome=verdict.value, confidence=finding["confidence"],
            finding_id=finding["finding_id"], actor=actor, source=source,
        )  # fmt: skip
