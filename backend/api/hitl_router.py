"""The human approval queue over HTTP."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.deps import Principal, get_services, require
from backend.container import Services
from backend.hitl.queue import DecisionConflict
from backend.models.schemas import HitlDecisionRequest

router = APIRouter(prefix="/api/hitl", tags=["hitl"])


@router.get("")
async def list_queue(
    status: str = "pending",
    limit: int = Query(100, ge=1, le=500),
    services: Services = Depends(get_services),
    _: Principal = Depends(require("hitl:read")),
) -> dict[str, Any]:
    """Waiting reviews, most urgent first (severity, then amount of work, then age)."""
    return {
        "items": await services.store.list_hitl(status, limit),
        "stats": await services.monitor.snapshot(),
        "alert_thresholds": {
            "escalation_rate": services.settings.escalation_rate_alert,
            "queue_depth": services.settings.hitl_queue_depth_alert,
        },
    }


@router.get("/{hitl_id}")
async def get_item(
    hitl_id: str,
    services: Services = Depends(get_services),
    _: Principal = Depends(require("hitl:read")),
) -> dict[str, Any]:
    try:
        item = await services.store.get_hitl(hitl_id)
    except Exception as exc:  # noqa: BLE001 - malformed id
        raise HTTPException(status_code=404, detail="queue item not found") from exc
    if item is None:
        raise HTTPException(status_code=404, detail="queue item not found")
    findings = await services.store.get_findings(item["review_id"])
    if item["kind"] == "dispute":
        findings = [f for f in findings if f["finding_id"] == item.get("finding_id")]
    return {"item": item, "review": await services.store.get_review(item["review_id"]), "findings": findings}


@router.post("/{hitl_id}/decision")
async def decide(
    hitl_id: str,
    request: HitlDecisionRequest,
    services: Services = Depends(get_services),
    principal: Principal = Depends(require("hitl:decide")),
) -> dict[str, Any]:
    """Approve or reject, finding by finding. Accepted findings are published.

    The reviewer is taken from the authenticated token, never from the request
    body, so the audit trail cannot be spoofed by a client.
    """
    try:
        return await services.approvals.decide(hitl_id, request, principal.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="queue item not found") from exc
    except DecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
