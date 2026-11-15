"""Economics: what the agents cost, read from the rollups."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from backend.api.deps import Principal, get_services, require
from backend.container import Services
from backend.economics.pricing import is_priced
from backend.economics.routing_advisor import advise

router = APIRouter(prefix="/api/economics", tags=["economics"])


@router.get("/summary")
async def summary(
    hours: int = Query(24, ge=1, le=24 * 30),
    services: Services = Depends(get_services),
    _: Principal = Depends(require("economics:read")),
) -> dict[str, Any]:
    s = services
    health = await s.store.agent_health(hours)
    budget = await s.budget.check()
    roles = ("security", "quality", "tests", "docs", "fallback")
    routing = {role: s.settings.model_for(role) for role in roles}
    return {
        "window_hours": hours,
        "budget": {
            "spent_today": round(budget.spent_today, 6),
            "daily_cap": budget.daily_cap,
            "per_review_cap": s.budget.per_review_cap,
            "ratio": round(budget.ratio, 4),
            "allowed": budget.allowed,
            "downgrade": budget.downgrade,
            "reason": budget.reason,
        },
        "agents": health,
        "timeseries": await s.store.cost_timeseries(max(hours, 48)),
        "top_reviews": await s.store.top_reviews_by_cost(max(hours, 168), 10),
        "advice": advise(
            health, agent_timeout_s=s.settings.agent_timeout_s, min_evidence=s.settings.feedback_min_evidence
        ),
        "routing": {
            "provider": s.settings.resolved_llm_provider,
            "models": routing,
            "unpriced": sorted({m for m in routing.values() if m and not is_priced(m)}),
        },
    }
