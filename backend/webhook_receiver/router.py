"""Webhook ingress: the receptionist.

It takes the order, checks it really came from GitHub, writes the ticket and
closes the door. Nothing slow happens here: GitHub waits about ten seconds
for an answer and a review takes far longer, so the work is queued and the
request is acknowledged at once.

Order of operations is an invariant (I1), not a style choice:

    size cap -> HMAC over the raw bytes -> headers -> JSON -> idempotency -> enqueue

Until the signature has verified, nothing in the request is parsed or trusted.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.api.deps import get_services
from backend.container import Services
from backend.core.exceptions import ConfigurationError, SignatureError
from backend.models.enums import EventType
from backend.webhook_receiver.parser import WebhookAction, parse_event
from backend.webhook_receiver.signature import SIGNATURE_HEADER, verify_signature

log = logging.getLogger(__name__)
router = APIRouter(tags=["webhook"])


def _reply(status_code: int, **body: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body)


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """Read the raw body, refusing anything larger than ``limit`` without buffering it all."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhooks/github")
async def github_webhook(request: Request, services: Services = Depends(get_services)) -> JSONResponse:
    settings = services.settings

    body = await _read_capped(request, settings.max_webhook_bytes)
    if body is None:
        return _reply(413, status="rejected", reason="payload too large")

    # 1. Authenticity first. A missing secret fails closed.
    try:
        verify_signature(settings.github_webhook_secret, body, request.headers.get(SIGNATURE_HEADER))
    except ConfigurationError as exc:
        log.error("webhook refused: %s", exc)
        return _reply(503, status="unavailable", reason="webhook secret is not configured")
    except SignatureError:
        # One fixed answer for every kind of bad signature: no oracle for an attacker.
        return _reply(401, status="rejected", reason="invalid signature")

    # 2. The sender is authenticated. Anything wrong from here on is a client error (4xx), not ours.
    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if not event or not delivery_id:
        return _reply(400, status="rejected", reason="missing X-GitHub-Event or X-GitHub-Delivery header")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _reply(400, status="rejected", reason="body is not valid JSON")
    if not isinstance(payload, dict):
        return _reply(400, status="rejected", reason="body must be a JSON object")

    # 3. Idempotency: GitHub redelivers, and a redelivery must not become a second review.
    claim_key = f"delivery:{delivery_id}"
    if not await services.idempotency.claim(claim_key, settings.idempotency_ttl_s):
        return _reply(200, status="duplicate", delivery_id=delivery_id)

    try:
        action = parse_event(event, payload, delivery_id, settings)
    except (KeyError, TypeError, ValueError) as exc:
        return _reply(400, status="rejected", reason=f"unexpected payload shape: {type(exc).__name__}")

    if action.kind in ("ignored", "ping"):
        await _record(services, delivery_id, event, payload, action.kind, action.reason, [])
        return _reply(200, status=action.kind, reason=action.reason, delivery_id=delivery_id)

    # 4. Enqueue and acknowledge. If the queue is down, release the claim and
    #    answer 503 so GitHub's own retry delivers the event again later.
    queued: list[dict[str, Any]] = []
    try:
        for item in [action, *action.extra]:
            accepted = await services.queue.enqueue(item.job, *item.args, job_id=item.job_id)
            queued.append({"job": item.job, "job_id": item.job_id, "accepted": accepted})
    except Exception as exc:  # noqa: BLE001
        log.exception("could not enqueue %s for delivery %s", action.job, delivery_id)
        await services.idempotency.release(claim_key)
        await _record(services, delivery_id, event, payload, "enqueue_failed", f"{type(exc).__name__}: {exc}", queued)
        return _reply(503, status="unavailable", reason="job queue unavailable; please redeliver")

    await _record(services, delivery_id, event, payload, action.kind, action.reason, queued)
    return _reply(202, status="queued", kind=action.kind, delivery_id=delivery_id, jobs=queued)


async def _record(
    services: Services, delivery_id: str, event: str, payload: dict[str, Any], outcome: str, reason: str,
    queued: list[dict[str, Any]],
) -> None:  # fmt: skip
    """Every delivery leaves a row on the event spine, including the ones we drop."""
    await services.emitter.emit(
        EventType.DECISION, agent="ingress", name="webhook", outcome=outcome, delivery_id=delivery_id,
        github_event=event, action=payload.get("action"), repo=(payload.get("repository") or {}).get("full_name"),
        reason=reason, jobs=queued,
    )  # fmt: skip


def describe(action: WebhookAction) -> dict[str, Any]:
    """Small helper for the CLI: what would this delivery do?"""
    return {"kind": action.kind, "job": action.job, "job_id": action.job_id, "reason": action.reason}
