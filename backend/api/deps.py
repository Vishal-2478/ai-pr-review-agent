"""Shared FastAPI dependencies: the service container and the caller's identity."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from backend.container import Services
from backend.security.rbac import role_allows


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="service is starting")
    return services


@dataclass(frozen=True)
class Principal:
    role: str
    name: str


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def get_principal(request: Request, services: Services = Depends(get_services)) -> Principal:
    settings = services.settings
    principals = settings.token_principals
    if not principals:
        if settings.is_production:
            # Fail closed: an unconfigured production dashboard is not an open one.
            raise HTTPException(status_code=503, detail="API_TOKENS is not configured")
        return Principal(role="admin", name="dev")  # local development convenience
    token = _bearer(request)
    match: Principal | None = None
    for known, (role, name) in principals.items():
        # Compare against every token in constant time; do not stop at the first hit.
        if hmac.compare_digest(token.encode(), known.encode()):
            match = Principal(role=role, name=name)
    if match is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return match


def require(permission: str) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not role_allows(principal.role, permission):
            raise HTTPException(status_code=403, detail=f"role '{principal.role}' may not {permission}")
        return principal

    return dependency
