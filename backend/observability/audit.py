"""Audit entries: who did what to which review.

They are ordinary rows on the append-only event spine, so the audit trail
inherits its immutability from the database triggers rather than from
application discipline.
"""

from __future__ import annotations

from typing import Any

from backend.models.enums import EventType
from backend.observability.events import EventEmitter


async def audit(emitter: EventEmitter, *, actor: str, action: str, review_id: str | None = None, **detail: Any) -> None:
    await emitter.emit(
        EventType.AUDIT, agent="human" if actor else "system", name=action, review_id=review_id,
        outcome="recorded", actor=actor or "system", **detail,
    )  # fmt: skip
