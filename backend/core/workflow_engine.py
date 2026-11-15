"""The one seam between application code and an orchestration framework.

ADR-001: LangGraph runs the review graph today, but nothing outside
``orchestrator/`` may import LangGraph. Application code depends on this
interface only, so moving to Temporal (or anything else) means adding one
implementation class, not rewriting callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkflowState:
    """Framework-neutral view of a workflow run."""

    workflow_id: str
    exists: bool
    finished: bool
    next_nodes: tuple[str, ...] = ()
    values: dict[str, Any] = field(default_factory=dict)


class WorkflowEngine(ABC):
    """Run, resume and inspect a review workflow keyed by ``workflow_id``."""

    name: str = "abstract"

    @abstractmethod
    async def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Start a workflow from the beginning and return its final state."""

    @abstractmethod
    async def resume(self, workflow_id: str) -> dict[str, Any]:
        """Continue an interrupted workflow from its last durable checkpoint."""

    @abstractmethod
    async def get_state(self, workflow_id: str) -> WorkflowState:
        """Describe where a workflow is without advancing it."""

    async def run_or_resume(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Crash-safe entry point used by the worker.

        A retried job must not redo completed work, so an unfinished checkpoint
        always wins over a fresh start.
        """
        state = await self.get_state(workflow_id)
        if state.exists and not state.finished:
            return await self.resume(workflow_id)
        if state.exists and state.finished:
            return state.values
        return await self.run(workflow_id, payload)

    async def forget(self, workflow_id: str) -> None:  # noqa: B027 - optional hook
        """Drop stored state for a workflow (used when a review is re-run on purpose)."""
