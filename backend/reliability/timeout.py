"""Per-node time budgets.

The fan-in join waits for every specialist. Without a deadline on each one, a
single hung model call would hang the whole review forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from backend.core.exceptions import NodeTimeoutError

T = TypeVar("T")


async def with_timeout(awaitable: Awaitable[T], seconds: float, label: str) -> T:
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise NodeTimeoutError(label, seconds) from exc
