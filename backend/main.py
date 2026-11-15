"""FastAPI application: webhook ingress plus the dashboard API.

Run with:  uvicorn backend.main:app --host 0.0.0.0 --port 8000

This process is the stateless ingress of the modular monolith. Reviews run in
the ARQ worker (``backend.job_queue.worker``); the two share one codebase and
one container image and scale independently.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import __version__
from backend.api import economics_router, hitl_router, reviews, system_router
from backend.config import Settings, get_settings
from backend.container import Services, build_services
from backend.observability.logging import configure_logging
from backend.webhook_receiver import router as webhook_router

log = logging.getLogger(__name__)


def create_app(services: Services | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the app. Pass ``services`` to run against prepared (for example in-memory) wiring."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = services is None
        cfg = settings or (services.settings if services else get_settings())
        configure_logging(cfg.log_level)
        app.state.services = services or await build_services(cfg, role="api")
        s = app.state.services
        if not cfg.github_webhook_secret:
            log.warning("GITHUB_WEBHOOK_SECRET is not set: every webhook will be refused")
        if not cfg.token_principals:
            log.warning(
                "API_TOKENS is not set: %s",
                "the dashboard API is disabled" if cfg.is_production else "the dashboard API is open (development)",
            )
        log.info(
            "api ready: engine=%s database=%s llm=%s autonomy=%s",
            s.engine.name, s.db_flavor, cfg.resolved_llm_provider, cfg.autonomy_level,
        )  # fmt: skip
        try:
            yield
        finally:
            if owned:
                await s.close()

    app = FastAPI(
        title="AI PR Review Agent",
        version=__version__,
        summary="Selective multi-agent pull request reviewer with a human approval queue.",
        lifespan=lifespan,
    )
    cfg = settings or (services.settings if services else get_settings())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to a caller; the detail goes to the log.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.include_router(webhook_router.router)
    app.include_router(system_router.router)
    app.include_router(reviews.router)
    app.include_router(hitl_router.router)
    app.include_router(economics_router.router)
    return app


app = create_app()
