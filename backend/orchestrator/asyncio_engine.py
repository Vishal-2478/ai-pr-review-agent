"""WorkflowEngine implemented with nothing but asyncio.

It exists for two reasons. It proves the WorkflowEngine seam is real: the
same nodes run under a second engine with no changes. And it is a dependable
fallback (WORKFLOW_ENGINE=asyncio) with no checkpointing: ``resume`` simply
runs the review again, which is safe because every node is idempotent.
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.core.workflow_engine import WorkflowEngine, WorkflowState
from backend.orchestrator.nodes import ReviewNodes


class AsyncioEngine(WorkflowEngine):
    name = "asyncio"

    def __init__(self, nodes: ReviewNodes) -> None:
        self._nodes = nodes
        self._runs: dict[str, dict[str, Any]] = {}

    async def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        n = self._nodes
        state: dict[str, Any] = {**payload, "agent_results": []}
        self._runs[workflow_id] = {"payload": dict(payload), "finished": False, "state": state}

        state.update(await n.build_context(state))
        branches = n.plan_fan_out(state)
        if not branches:
            state.update(await n.finalize(state))
        else:
            # Fan-out / fan-in: gather returns only when every branch has produced a result.
            for update in await asyncio.gather(*(n.specialist(b) for b in branches)):
                state["agent_results"].extend(update["agent_results"])
            state.update(await n.aggregate(state))
            ending = getattr(n, n.route_after_gate(state))
            state.update(await ending(state))

        self._runs[workflow_id]["finished"] = True
        return state

    async def resume(self, workflow_id: str) -> dict[str, Any]:
        run = self._runs.get(workflow_id)
        if run is None:
            raise KeyError(f"no workflow {workflow_id} to resume")
        return await self.run(workflow_id, run["payload"])

    async def get_state(self, workflow_id: str) -> WorkflowState:
        run = self._runs.get(workflow_id)
        if run is None:
            return WorkflowState(workflow_id=workflow_id, exists=False, finished=False)
        return WorkflowState(workflow_id=workflow_id, exists=True, finished=run["finished"], values=run["state"])

    async def forget(self, workflow_id: str) -> None:
        self._runs.pop(workflow_id, None)
