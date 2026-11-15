"""Per-agent model routing.

Different concerns deserve different models: a missed injection is expensive,
a missed docstring is not. The router picks the configured model for the
agent, then adjusts for the situation: an open circuit, a nearly spent
budget, or a diff too small to justify the larger model.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.config import Settings
from backend.economics.budget_guard import BudgetStatus
from backend.reliability.circuit_breaker import BreakerRegistry

SMALL_DIFF_CHARS = 1200
_DOWNGRADABLE_WHEN_SMALL = {"tests", "docs"}


@dataclass(frozen=True)
class ModelChoice:
    models: tuple[str, ...]  # tried in order
    reason: str

    @property
    def primary(self) -> str:
        return self.models[0]


class ModelRouter:
    def __init__(self, settings: Settings, breakers: BreakerRegistry) -> None:
        self._settings = settings
        self._breakers = breakers

    def select(self, agent: str, *, diff_chars: int, budget: BudgetStatus | None = None) -> ModelChoice:
        primary = self._settings.model_for(agent)
        fallback = self._settings.model_for("fallback")
        if primary == fallback:
            return ModelChoice((primary,), "configured model")
        if self._breakers.get(f"llm:{primary}").state == "open":
            return ModelChoice((fallback,), f"circuit open for {primary}")
        if budget is not None and budget.downgrade:
            return ModelChoice((fallback, primary), f"budget at {budget.ratio:.0%} of daily cap")
        if agent in _DOWNGRADABLE_WHEN_SMALL and diff_chars <= SMALL_DIFF_CHARS:
            return ModelChoice((fallback, primary), "small diff")
        return ModelChoice((primary, fallback), "configured model")
