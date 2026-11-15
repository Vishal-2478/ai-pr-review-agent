"""Health, readiness and the operator's view of the system."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend import __version__
from backend.api.deps import Principal, get_principal, get_services, require
from backend.container import Services
from backend.observability.audit import audit

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. No dependencies are touched."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
async def readyz(services: Services = Depends(get_services)) -> JSONResponse:
    """Readiness: can this instance accept a webhook and queue the work?"""
    checks: dict[str, str] = {}
    try:
        checks["database"] = "ok" if await services.store.ping() else "failing"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"failing: {type(exc).__name__}"
    if services.redis is not None:
        try:
            await services.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"failing: {type(exc).__name__}"
    else:
        checks["redis"] = "not configured (in-process queue)"
    checks["webhook_secret"] = "ok" if services.settings.github_webhook_secret else "missing"
    ready = all(v == "ok" or v.startswith("not configured") for v in checks.values())
    return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "checks": checks})


@router.get("/api/me")
async def me(principal: Principal = Depends(get_principal)) -> dict[str, str]:
    return {"role": principal.role, "name": principal.name}


@router.get("/api/system/status")
async def status(
    services: Services = Depends(get_services),
    _: Principal = Depends(require("reviews:read")),
) -> dict[str, Any]:
    s = services
    settings = s.settings
    prompts = {}
    for name in ("base", "security", "quality", "tests", "docs", "judge"):
        try:
            prompts[name] = s.prompts.get(name).ref
        except Exception:  # noqa: BLE001 - a missing optional template is not an outage
            prompts[name] = None
    return {
        "version": __version__,
        "environment": settings.environment,
        "engine": s.engine.name,
        "database": s.db_flavor,
        "llm_provider": settings.resolved_llm_provider,
        "embedding_provider": settings.resolved_embedding_provider,
        "autonomy_level": settings.autonomy_level,
        "thresholds": {
            "hitl_confidence_threshold": settings.hitl_confidence_threshold,
            "noise_floor": settings.noise_floor,
            "max_inline_comments": settings.max_inline_comments,
        },
        "prompts": prompts,
        "queue": await s.queue.stats(),
        "humans": await s.monitor.snapshot(),
        "circuit_breakers": s.breakers.snapshot(),
        "events_dropped": s.emitter.dropped,
        "recent_alerts": s.alerter.sent[-10:],
    }


@router.get("/api/calibration")
async def calibration(
    repo: str = Query(..., description="owner/name"),
    services: Services = Depends(get_services),
    _: Principal = Depends(require("reviews:read")),
) -> dict[str, Any]:
    """What human feedback has taught the system about this repository, and the evidence behind it."""
    return {
        "repo": repo,
        "min_evidence": services.calibrator.min_evidence,
        "min_actors": services.calibrator.min_actors,
        "half_life_days": services.calibrator.half_life_days,
        "buckets": await services.calibrator.explain(repo),
    }


class IndexRequest(BaseModel):
    repo: str
    ref: str = "HEAD"
    installation_id: int | None = None


@router.post("/api/index", status_code=202)
async def index_repo(
    request: IndexRequest,
    services: Services = Depends(get_services),
    principal: Principal = Depends(require("index:write")),
) -> dict[str, Any]:
    """Build or refresh the semantic memory of a repository."""
    if not services.settings.repo_allowed(request.repo):
        raise HTTPException(status_code=403, detail="repository is not in ALLOWED_REPOS")
    await audit(services.emitter, actor=principal.name, action="index_requested", repo=request.repo, ref=request.ref)
    accepted = await services.queue.enqueue(
        "index_repository", request.repo, request.ref, request.installation_id, None,
        job_id=f"index:{request.repo}:{request.ref}",
    )  # fmt: skip
    return {"repo": request.repo, "queued": accepted, "index": await services.store.index_stats(request.repo)}
