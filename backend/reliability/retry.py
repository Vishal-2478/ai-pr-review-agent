"""Retry with exponential backoff and full jitter.

Only errors flagged retryable are retried; a 401 from GitHub or a schema
violation will never get better by asking again.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from backend.core.exceptions import CircuitOpenError, ReviewAgentError

T = TypeVar("T")


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, CircuitOpenError):
        return False
    if isinstance(exc, ReviewAgentError):
        return exc.retryable
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError))


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Full jitter: uniform in [0, min(cap, base * 2**attempt)]."""
    return random.uniform(0, min(cap, base * (2**attempt)))  # noqa: S311 - jitter, not crypto


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: Callable[[BaseException], bool] = is_retryable,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    last: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            if attempt >= attempts - 1 or not retry_on(exc):
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            await sleep(delay)
    raise last  # pragma: no cover - loop always returns or raises
