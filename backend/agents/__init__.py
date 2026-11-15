"""Four specialists, one concern each, plus the aggregator that merges them."""

from __future__ import annotations

from backend.agents.base_agent import AgentDeps, AgentInput, SpecialistAgent
from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.tests_agent import TestsAgent
from backend.models.enums import AgentType

SPECIALISTS: dict[AgentType, type[SpecialistAgent]] = {
    AgentType.SECURITY: SecurityAgent,
    AgentType.QUALITY: QualityAgent,
    AgentType.TESTS: TestsAgent,
    AgentType.DOCS: DocsAgent,
}


def build_specialists(deps: AgentDeps) -> dict[str, SpecialistAgent]:
    return {agent.value: cls(deps) for agent, cls in SPECIALISTS.items()}


__all__ = ["AgentDeps", "AgentInput", "SpecialistAgent", "SPECIALISTS", "build_specialists"]
