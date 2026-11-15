"""The event spine writer.

Every span of work, model call, tool call, decision and escalation becomes one
append-only row in ``agent_events``. That one stream answers three questions
later: what happened (trace viewer), why was this said (audit trail), and what
did it cost (economics).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.models.enums import EventType
from backend.models.schemas import Event
from backend.observability.context import current_review_id, current_span_id
from backend.security.secret_masking import mask_obj

log = logging.getLogger("events")


class EventEmitter:
    def __init__(self, store: Any, tracer: Any | None = None) -> None:
        self._store = store
        self._tracer = tracer
        self.dropped = 0

    async def emit(
        self,
        event_type: EventType | str,
        *,
        agent: str = "system",
        name: str = "",
        review_id: str | None = None,
        span_id: str | None = None,
        parent_span: str | None = None,
        model: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int | None = None,
        outcome: str | None = None,
        confidence: float | None = None,
        **payload: Any,
    ) -> None:
        event = Event(
            ts=datetime.now(UTC).isoformat(),
            review_id=review_id or current_review_id.get(),
            agent=agent,
            span_id=span_id or current_span_id.get() or "",
            parent_span=parent_span,
            event_type=event_type.value if isinstance(event_type, EventType) else event_type,
            name=name,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            outcome=outcome,
            confidence=confidence,
            payload=mask_obj(_jsonable(payload)),  # type: ignore[arg-type]
        )
        try:
            await self._store.insert_events([event])
        except Exception as exc:  # noqa: BLE001
            # Observability must never take a review down, but an event must not
            # vanish either: the structured log line is the fallback sink.
            self.dropped += 1
            log.error("event_write_failed %s event=%s", exc, event.model_dump_json())

    def span(self, name: str, *, agent: str = "system", review_id: str | None = None, **payload: Any) -> Span:
        return Span(self, name, agent, review_id, payload)


class Span:
    """``async with emitter.span("retrieve", agent="security") as span: ...``"""

    def __init__(self, emitter: EventEmitter, name: str, agent: str, review_id: str | None, payload: dict[str, Any]) -> None:
        self._emitter = emitter
        self.name = name
        self.agent = agent
        self.review_id = review_id
        self.span_id = uuid.uuid4().hex[:16]
        self.parent: str | None = None
        self.outcome = "ok"
        self.confidence: float | None = None
        self._payload = payload
        self._end_payload: dict[str, Any] = {}
        self._token: Any = None
        self._review_token: Any = None
        self._t0 = 0.0
        self._otel: Any = None

    def set(self, **payload: Any) -> None:
        self._end_payload.update(payload)

    async def __aenter__(self) -> Span:
        self.parent = current_span_id.get()
        self._token = current_span_id.set(self.span_id)
        if self.review_id:
            self._review_token = current_review_id.set(self.review_id)
        self._t0 = time.perf_counter()
        if self._emitter._tracer is not None:
            self._otel = self._emitter._tracer.start_span(f"{self.agent}.{self.name}")
        await self._emitter.emit(
            EventType.SPAN_START, agent=self.agent, name=self.name, review_id=self.review_id,
            span_id=self.span_id, parent_span=self.parent, **self._payload,
        )  # fmt: skip
        return self

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        latency = int((time.perf_counter() - self._t0) * 1000)
        if exc is not None:
            self.outcome = "cancelled" if exc_type.__name__ == "CancelledError" else "error"
            self._end_payload.setdefault("error", f"{exc_type.__name__}: {exc}"[:500])
        try:
            await self._emitter.emit(
                EventType.SPAN_END, agent=self.agent, name=self.name, review_id=self.review_id,
                span_id=self.span_id, parent_span=self.parent, latency_ms=latency, outcome=self.outcome,
                confidence=self.confidence, **self._end_payload,
            )  # fmt: skip
        finally:
            if self._otel is not None:
                self._otel.set_attribute("outcome", self.outcome)
                self._otel.end()
            current_span_id.reset(self._token)
            if self._review_token is not None:
                current_review_id.reset(self._review_token)


def _jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))
