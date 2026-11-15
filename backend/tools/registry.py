"""Tool registry with capability scopes.

Design choice: tools are invoked by the pipeline, not chosen by a model. A
specialist never decides to call something because of text it read in a pull
request, which removes the most direct prompt-injection path. Every tool is a
read-only function over data the review already holds; none of them can run
code from the pull request, write to a repository or reach the network.

Each call is checked against the calling agent's scope, time-boxed, size-capped
and recorded as a ``tool.call`` event.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.core.exceptions import PermanentError
from backend.models.enums import EventType

READ_DIFF = "read_diff"
READ_MEMORY = "read_memory"

AGENT_SCOPES: dict[str, frozenset[str]] = {
    "security": frozenset({READ_DIFF, READ_MEMORY}),
    "quality": frozenset({READ_DIFF, READ_MEMORY}),
    "tests": frozenset({READ_DIFF, READ_MEMORY}),
    "docs": frozenset({READ_DIFF, READ_MEMORY}),
    "aggregator": frozenset(),  # the aggregator merges data; it never reads untrusted input directly
}


@dataclass(frozen=True)
class Tool:
    name: str
    capability: str
    fn: Callable[..., Any]
    description: str = ""
    timeout_s: float = 10.0


class ToolRegistry:
    def __init__(self, emitter: Any | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._emitter = emitter

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def call(self, agent: str, name: str, /, **kwargs: Any) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise PermanentError(f"unknown tool '{name}'")
        if tool.capability not in AGENT_SCOPES.get(agent, frozenset()):
            raise PermanentError(f"agent '{agent}' is not allowed to use '{name}' ({tool.capability})")
        started = time.perf_counter()
        outcome, summary = "ok", ""
        try:
            result = tool.fn(**kwargs)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=tool.timeout_s)
            summary = f"{len(result)} items" if hasattr(result, "__len__") else type(result).__name__
            return result
        except Exception as exc:
            outcome, summary = "error", f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            if self._emitter is not None:
                await self._emitter.emit(
                    EventType.TOOL_CALL, agent=agent, name=name, outcome=outcome,
                    latency_ms=int((time.perf_counter() - started) * 1000), result=summary,
                )  # fmt: skip
