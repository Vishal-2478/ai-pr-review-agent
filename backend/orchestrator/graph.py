"""The review workflow as a LangGraph StateGraph.

    START -> build_context -+-> specialist(security) -+
                            +-> specialist(quality)  -+-> aggregate -+-> publish     -> END
                            +-> specialist(tests)    -+              +-> queue_human -> END
                            +-> specialist(docs)     -+              +-> finalize    -> END

Fan-out uses the Send API: one ``Send`` per specialist, each with its own
payload, all executed in the same super-step. The plain edge
``specialist -> aggregate`` is the fan-in: ``aggregate`` runs once, after
every branch of that super-step has written to ``agent_results``. The join is
encoded in the graph rather than in hand-written counting code.

This is the only module (with ``langgraph_engine`` and the checkpointer) that
imports LangGraph.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from backend.orchestrator.nodes import ReviewNodes
from backend.orchestrator.state import ReviewState


def build_graph(nodes: ReviewNodes, checkpointer: Any | None = None) -> Any:
    def fan_out(state: ReviewState) -> list[Send] | str:
        payloads = nodes.plan_fan_out(dict(state))
        if not payloads:
            return "finalize"
        return [Send("specialist", payload) for payload in payloads]

    graph = StateGraph(ReviewState)
    graph.add_node("build_context", nodes.build_context)
    graph.add_node("specialist", nodes.specialist)
    graph.add_node("aggregate", nodes.aggregate)
    graph.add_node("publish", nodes.publish)
    graph.add_node("queue_human", nodes.queue_human)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "build_context")
    graph.add_conditional_edges("build_context", fan_out, ["specialist", "finalize"])
    graph.add_edge("specialist", "aggregate")
    graph.add_conditional_edges(
        "aggregate",
        nodes.route_after_gate,
        {"publish": "publish", "queue_human": "queue_human", "finalize": "finalize"},
    )
    graph.add_edge("publish", END)
    graph.add_edge("queue_human", END)
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
