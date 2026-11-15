"""Reviews, findings and traces: the read side of the truth and time lanes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.deps import Principal, get_services, require
from backend.container import Services
from backend.models.enums import EventType, FindingStatus, ReviewStatus, Verdict
from backend.models.schemas import FeedbackRequest, PRContext
from backend.observability.audit import audit

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


async def _review_or_404(services: Services, review_id: str) -> dict[str, Any]:
    try:
        review = await services.store.get_review(review_id)
    except Exception as exc:  # noqa: BLE001 - a malformed id is a 404, not a 500
        raise HTTPException(status_code=404, detail="review not found") from exc
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    return review


@router.get("")
async def list_reviews(
    status: str | None = None,
    repo: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    services: Services = Depends(get_services),
    _: Principal = Depends(require("reviews:read")),
) -> dict[str, Any]:
    return {"reviews": await services.store.list_reviews(limit=limit, status=status, repo=repo)}


@router.get("/{review_id}")
async def get_review(
    review_id: str,
    services: Services = Depends(get_services),
    _: Principal = Depends(require("reviews:read")),
) -> dict[str, Any]:
    review = await _review_or_404(services, review_id)
    findings = await services.store.get_findings(review_id)
    pending = [h for h in await services.store.list_hitl("pending", 200) if h["review_id"] == review_id]
    return {"review": review, "findings": findings, "hitl": pending}


@router.get("/{review_id}/trace")
async def get_trace(
    review_id: str,
    services: Services = Depends(get_services),
    _: Principal = Depends(require("reviews:read")),
) -> dict[str, Any]:
    """Every event of one review, in time order: reconstruct what the agent did and why."""
    review = await _review_or_404(services, review_id)
    events = await services.store.get_trace(review_id)
    return {
        "review_id": review_id,
        "status": review["status"],
        "events": events,
        "totals": {
            "events": len(events),
            "llm_calls": sum(1 for e in events if e["event_type"] == EventType.LLM_CALL.value),
            "cost_usd": round(sum(float(e.get("cost_usd") or 0) for e in events), 6),
            "tokens_in": sum(int(e.get("tokens_in") or 0) for e in events),
            "tokens_out": sum(int(e.get("tokens_out") or 0) for e in events),
            "errors": sum(1 for e in events if e["event_type"] == EventType.ERROR.value),
        },
    }


@router.post("/{review_id}/rerun", status_code=202)
async def rerun_review(
    review_id: str,
    services: Services = Depends(get_services),
    principal: Principal = Depends(require("reviews:rerun")),
) -> dict[str, Any]:
    """Run a failed, skipped or superseded review again. Finished reviews are never redone."""
    review = await _review_or_404(services, review_id)
    rerunnable = {ReviewStatus.FAILED.value, ReviewStatus.SKIPPED.value, ReviewStatus.SUPERSEDED.value}
    if review["status"] not in rerunnable:
        raise HTTPException(status_code=409, detail=f"a review in status '{review['status']}' cannot be re-run")
    pr = PRContext(
        repo=review["repo"], pr_number=review["pr_number"], head_sha=review["head_sha"],
        base_sha=review.get("base_sha") or "", title=review.get("title") or "", author=review.get("author") or "",
        html_url=review.get("html_url") or "", installation_id=review.get("installation_id"),
        delivery_id=f"rerun-by-{principal.name}",
    )  # fmt: skip
    await audit(services.emitter, actor=principal.name, action="review_rerun", review_id=review_id)
    accepted = await services.queue.enqueue(
        "review_pull_request", {"pr": pr.model_dump(), "force": True}, job_id=f"rerun:{review_id}:{review['updated_at']}"
    )
    return {"review_id": review_id, "queued": accepted}


@router.post("/{review_id}/findings/{finding_id}/feedback")
async def give_feedback(
    review_id: str,
    finding_id: str,
    request: FeedbackRequest,
    services: Services = Depends(get_services),
    principal: Principal = Depends(require("feedback:write")),
) -> dict[str, Any]:
    """Record a human verdict on one finding outside the approval queue.

    This is the learning loop's input for auto-posted findings: a maintainer
    marking one as wrong (or right) calibrates future confidence.
    """
    try:
        verdict = Verdict(request.verdict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="verdict must be accepted, rejected or disputed") from exc
    await _review_or_404(services, review_id)
    finding = next((f for f in await services.store.get_findings(review_id) if f["finding_id"] == finding_id), None)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    await services.store.add_feedback(
        review_id=review_id, finding_id=finding_id, repo=finding["repo"], agent_type=finding["agent_type"],
        category=finding["category"], verdict=verdict.value, actor=principal.name, source="dashboard",
        note=request.note,
    )  # fmt: skip
    if verdict is Verdict.REJECTED and finding["status"] == FindingStatus.POSTED.value:
        await services.store.set_finding_status(review_id, [finding_id], FindingStatus.RETRACTED.value)
    await services.emitter.emit(
        EventType.FEEDBACK, agent=finding["agent_type"], name=finding["category"], review_id=review_id,
        outcome=verdict.value, confidence=finding["confidence"], finding_id=finding_id,
        actor=principal.name, source="dashboard",
    )  # fmt: skip
    await audit(
        services.emitter, actor=principal.name, action=f"feedback_{verdict.value}", review_id=review_id,
        finding_id=finding_id, note=request.note,
    )  # fmt: skip
    return {"finding_id": finding_id, "verdict": verdict.value}
