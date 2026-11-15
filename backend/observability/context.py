"""Ambient trace context, carried by contextvars across awaits and tasks."""

from __future__ import annotations

from contextvars import ContextVar

current_review_id: ContextVar[str | None] = ContextVar("current_review_id", default=None)
current_span_id: ContextVar[str | None] = ContextVar("current_span_id", default=None)
