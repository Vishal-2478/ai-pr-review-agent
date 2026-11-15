"""Workflow state.

Plain JSON-shaped data only (dicts, lists, strings, numbers). That keeps the
state serialisable by any checkpointer and readable by any engine: nothing in
here is a LangGraph type.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class ReviewState(TypedDict, total=False):
    review_id: str
    pr: dict[str, Any]  # PRContext
    files: list[dict[str, Any]]  # DiffFile[]
    context: dict[str, Any]  # RetrievedContext
    skip_reason: str
    # Fan-in channel: each specialist appends exactly one AgentResult. The
    # reducer (list concatenation) is what lets four parallel branches write
    # to the same key without overwriting one another.
    agent_results: Annotated[list[dict[str, Any]], operator.add]
    outcome: dict[str, Any]  # GateOutcome + critical summaries
    final_status: str
