"""The learning loop, with its guard rails.

Every human verdict (accept, reject, dispute) is feedback. Used naively,
feedback is also an attack surface and a source of drift: one grumpy reviewer,
or one person who disputes everything, could teach the agent to stay quiet.
So feedback changes behaviour only when:

- **minimum evidence**: at least N verdicts exist for that agent and category;
- **more than one voice**: they come from at least M distinct people;
- **recency**: old verdicts decay with a half-life, so the agent is not
  punished forever for a prompt that has since been fixed.

Calibration only ever lowers confidence. Raising it from feedback would let a
streak of approvals talk the agent into bypassing the human gate.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

NEGATIVE_WEIGHT = {"rejected": 1.0, "disputed": 0.5}  # a dispute is a claim, not yet a verdict
MIN_MULTIPLIER = 0.5


class FeedbackCalibrator:
    def __init__(self, store: Any, min_evidence: int = 5, min_actors: int = 2, half_life_days: float = 30.0) -> None:
        self._store = store
        self.min_evidence = min_evidence
        self.min_actors = min_actors
        self.half_life_days = half_life_days

    async def explain(self, repo: str) -> list[dict[str, Any]]:
        """Per (agent, category): the evidence and the multiplier it earns."""
        rows = await self._store.feedback_rows(repo)
        now = datetime.now(UTC)
        buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"n": 0, "actors": set(), "positive": 0.0, "negative": 0.0}
        )
        for row in rows:
            created = row["created_at"]
            created_dt = datetime.fromisoformat(created) if isinstance(created, str) else created
            age_days = max(0.0, (now - created_dt).total_seconds() / 86400)
            weight = 0.5 ** (age_days / self.half_life_days)
            for key in ((row["agent_type"], row["category"]), (row["agent_type"], "*")):
                b = buckets[key]
                b["n"] += 1
                if row["actor"]:
                    b["actors"].add(row["actor"])
                if row["verdict"] == "accepted":
                    b["positive"] += weight
                else:
                    b["negative"] += weight * NEGATIVE_WEIGHT.get(row["verdict"], 1.0)

        out = []
        for (agent, category), b in sorted(buckets.items()):
            enough = b["n"] >= self.min_evidence and len(b["actors"]) >= self.min_actors
            # Laplace-smoothed acceptance rate; at or above 50% earns no penalty.
            rate = (b["positive"] + 1.0) / (b["positive"] + b["negative"] + 2.0)
            multiplier = max(MIN_MULTIPLIER, min(1.0, 0.5 + rate)) if enough else 1.0
            out.append(
                {
                    "agent": agent, "category": category, "verdicts": b["n"], "actors": len(b["actors"]),
                    "acceptance_rate": round(rate, 3), "multiplier": round(multiplier, 3),
                    "applied": enough and multiplier < 1.0,
                    "reason": "" if enough else f"needs {self.min_evidence} verdicts from {self.min_actors} people",
                }  # fmt: skip
            )
        return out

    async def multipliers(self, repo: str) -> dict[tuple[str, str], float]:
        return {(e["agent"], e["category"]): e["multiplier"] for e in await self.explain(repo) if e["applied"]}
