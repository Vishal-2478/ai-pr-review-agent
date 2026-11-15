"""Security specialist: could this change be exploited?"""

from __future__ import annotations

from backend.agents.base_agent import SpecialistAgent
from backend.models.enums import AgentType, Severity


class SecurityAgent(SpecialistAgent):
    agent_type = AgentType.SECURITY
    max_severity = Severity.CRITICAL  # the only specialist allowed to raise a critical

    # Reads every file: a leaked key in a README or a test fixture is still leaked.
