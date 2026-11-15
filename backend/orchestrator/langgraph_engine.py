"""WorkflowEngine implemented with LangGraph (ADR-001).

Swapping orchestrators means writing a sibling of this file. Nothing outside
``orchestrator/`` knows LangGraph exists.
"""

from __future__ import annotations

from typing import Any

from backend.core.workflow_engine import WorkflowEngine, WorkflowState
from backend.orchestrator.graph import build_graph
from backend.orchestrator.nodes import ReviewNodes


class LangGraphEngine(WorkflowEngine):
    name = "langgraph"

    def __init__(self, nodes: ReviewNodes, checkpointer: Any) -> None:
        self._checkpointer = checkpointer
        self._graph = build_graph(nodes, checkpointer)

    @staticmethod
    def _config(workflow_id: str) -> dict[str, Any]:
        # thread_id = review id: one durable thread of checkpoints per review.
        return {"configurable": {"thread_id": workflow_id}, "recursion_limit": 25}

    async def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # durability="sync": the checkpoint is on disk before the next step starts,
        # so a crash can never lose a node that already reported success.
        return await self._graph.ainvoke(payload, self._config(workflow_id), durability="sync")

    async def resume(self, workflow_id: str) -> dict[str, Any]:
        # Input None = continue from the last checkpoint. Tasks whose writes were
        # already saved are not executed again.
        return await self._graph.ainvoke(None, self._config(workflow_id), durability="sync")

    async def get_state(self, workflow_id: str) -> WorkflowState:
        snapshot = await self._graph.aget_state(self._config(workflow_id))
        exists = snapshot.created_at is not None
        return WorkflowState(
            workflow_id=workflow_id,
            exists=exists,
            finished=exists and not snapshot.next,
            next_nodes=tuple(snapshot.next or ()),
            values=dict(snapshot.values or {}),
        )

    async def forget(self, workflow_id: str) -> None:
        await self._checkpointer.adelete_thread(workflow_id)
