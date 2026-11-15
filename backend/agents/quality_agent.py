"""Quality specialist: is the logic right?"""

from __future__ import annotations

from backend.agents.base_agent import SpecialistAgent, source_and_tests
from backend.models.enums import AgentType, Severity
from backend.models.schemas import DiffFile


class QualityAgent(SpecialistAgent):
    agent_type = AgentType.QUALITY
    max_severity = Severity.HIGH

    def relevant_files(self, files: list[DiffFile]) -> list[DiffFile]:
        return source_and_tests(files)
