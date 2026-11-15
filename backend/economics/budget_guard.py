"""BudgetGuard (ADR-004): check the money before spending it.

Runs at the start of every specialist. It reads the day's running total from
the ``agent_health_1m`` rollup, never by scanning raw events, and answers
before any model is called. A blocked specialist does not fail the review:
it falls back to the free deterministic rule pack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetStatus:
    allowed: bool
    spent_today: float
    daily_cap: float
    review_spent: float = 0.0
    reason: str = ""
    downgrade: bool = False

    @property
    def ratio(self) -> float:
        return self.spent_today / self.daily_cap if self.daily_cap > 0 else 0.0


class BudgetGuard:
    def __init__(self, store: Any, daily_cap: float, per_review_cap: float, downgrade_ratio: float = 0.8) -> None:
        self._store = store
        self.daily_cap = daily_cap
        self.per_review_cap = per_review_cap
        self.downgrade_ratio = downgrade_ratio

    async def check(self, review_id: str | None = None) -> BudgetStatus:
        try:
            spent = await self._store.cost_today()
            review_spent = await self._store.review_cost(review_id) if review_id else 0.0
        except Exception as exc:  # noqa: BLE001
            # Fail closed: if spend cannot be verified, no paid call is made.
            log.error("budget check failed, blocking paid calls: %s", exc)
            return BudgetStatus(False, 0.0, self.daily_cap, reason=f"budget unavailable: {exc}")
        if self.daily_cap > 0 and spent >= self.daily_cap:
            return BudgetStatus(False, spent, self.daily_cap, review_spent, f"daily cap ${self.daily_cap:.2f} reached")
        if self.per_review_cap > 0 and review_spent >= self.per_review_cap:
            return BudgetStatus(
                False, spent, self.daily_cap, review_spent, f"per-review cap ${self.per_review_cap:.2f} reached"
            )
        downgrade = self.daily_cap > 0 and spent >= self.daily_cap * self.downgrade_ratio
        return BudgetStatus(True, spent, self.daily_cap, review_spent, downgrade=downgrade)
