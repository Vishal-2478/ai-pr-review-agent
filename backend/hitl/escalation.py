"""Capacity planning for the humans in the loop.

A queue that grows faster than people clear it is a failure mode of its own:
approvals go stale and the agent's output stops mattering. This monitor
watches three signals on the truth tables and alerts when the human side of
the system is the bottleneck:

- escalation rate: the share of reviews the agent could not handle alone;
- queue depth: how many reviews are waiting;
- queue age: how long the oldest one has waited.

A high escalation rate is not solved by more reviewers. It means the agent
needs work (prompt, retrieval, thresholds), and the alert says so.
"""

from __future__ import annotations

import time
from typing import Any

from backend.config import Settings
from backend.observability.alerting import Alerter

MIN_DECISIONS_FOR_RATE = 10
STALE_AFTER_S = 24 * 3600
REALERT_AFTER_S = 6 * 3600


def escalation_rate(decisions: dict[str, int]) -> tuple[float, int]:
    total = sum(decisions.values())
    deferred = decisions.get("human_review", 0) + decisions.get("escalate", 0)
    return (deferred / total if total else 0.0), total


class QueueMonitor:
    def __init__(self, store: Any, alerter: Alerter, settings: Settings) -> None:
        self._store = store
        self._alerter = alerter
        self._settings = settings
        self._last_alert: dict[str, float] = {}

    async def snapshot(self) -> dict[str, Any]:
        stats = await self._store.hitl_stats()
        decisions = await self._store.decision_stats(24)
        rate, total = escalation_rate(decisions)
        return {**stats, "decisions_24h": decisions, "reviews_24h": total, "escalation_rate_24h": round(rate, 3)}

    async def check(self) -> list[str]:
        snap = await self.snapshot()
        raised: list[str] = []
        s = self._settings
        if snap["reviews_24h"] >= MIN_DECISIONS_FOR_RATE and snap["escalation_rate_24h"] > s.escalation_rate_alert:
            raised.append("escalation_rate")
            await self._alert(
                "escalation_rate", "Agent is deferring too much to humans",
                f"{snap['escalation_rate_24h']:.0%} of {snap['reviews_24h']} reviews in the last 24h needed a human "
                f"(alert threshold {s.escalation_rate_alert:.0%}). Look at per-agent rejection rates and recent "
                "low-confidence findings before adding reviewers.",
            )  # fmt: skip
        if snap["pending"] > s.hitl_queue_depth_alert:
            raised.append("queue_depth")
            await self._alert(
                "queue_depth", "Approval queue is growing faster than it is cleared",
                f"{snap['pending']} reviews are waiting ({snap['resolved_24h']} resolved in the last 24h).",
            )  # fmt: skip
        if snap["oldest_pending_s"] > STALE_AFTER_S:
            raised.append("queue_age")
            await self._alert(
                "queue_age", "A review has been waiting for a human for over a day",
                f"Oldest pending item is {snap['oldest_pending_s'] / 3600:.0f}h old.",
            )  # fmt: skip
        return raised

    async def _alert(self, key: str, title: str, text: str) -> None:
        now = time.monotonic()
        if now - self._last_alert.get(key, -REALERT_AFTER_S) < REALERT_AFTER_S:
            return
        self._last_alert[key] = now
        await self._alerter.notify(title, text, severity="warning")
