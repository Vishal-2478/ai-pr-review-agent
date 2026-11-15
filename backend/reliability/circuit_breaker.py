"""Circuit breaker: stop hammering a dependency that is already down.

closed  -> calls pass; consecutive failures are counted
open    -> calls fail fast with CircuitOpenError until ``recovery_s`` elapses
half    -> one probe call is allowed; success closes, failure re-opens

State is per process. With several workers each learns independently, which
is acceptable here: the cost of a slow learner is a handful of extra failed
calls, and it keeps Redis out of the hot path.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from backend.core.exceptions import CircuitOpenError

T = TypeVar("T")


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 4,
        recovery_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.recovery_s:
            return "half_open"
        return "open"

    def allow(self) -> None:
        state = self.state
        if state == "open":
            assert self._opened_at is not None
            raise CircuitOpenError(self.name, self.recovery_s - (self._clock() - self._opened_at))
        if state == "half_open":
            if self._probing:
                raise CircuitOpenError(self.name, self.recovery_s)
            self._probing = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._failures += 1
        self._probing = False
        if self._failures >= self.failure_threshold or self._opened_at is not None:
            self._opened_at = self._clock()

    async def call(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        counts_as_failure: Callable[[BaseException], bool] | None = None,
    ) -> T:
        self.allow()
        try:
            result = await fn()
        except Exception as exc:
            # A caller error (bad request) says nothing about the dependency's health.
            if counts_as_failure is None or counts_as_failure(exc):
                self.record_failure()
            else:
                self._probing = False
            raise
        self.record_success()
        return result

    def snapshot(self) -> dict[str, object]:
        return {"name": self.name, "state": self.state, "failures": self._failures}


class BreakerRegistry:
    def __init__(self, failure_threshold: int = 4, recovery_s: float = 30.0) -> None:
        self._defaults = (failure_threshold, recovery_s)
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, *self._defaults)
        return self._breakers[name]

    def snapshot(self) -> list[dict[str, object]]:
        return [b.snapshot() for b in self._breakers.values()]
