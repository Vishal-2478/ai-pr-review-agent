"""Testing specialist: what is untested?"""

from __future__ import annotations

from backend.agents.base_agent import SpecialistAgent, source_and_tests
from backend.models.enums import AgentType, Severity
from backend.models.schemas import DiffFile


class TestsAgent(SpecialistAgent):
    __test__ = False  # not a pytest class, despite the name
    agent_type = AgentType.TESTS
    max_severity = Severity.HIGH

    def relevant_files(self, files: list[DiffFile]) -> list[DiffFile]:
        return source_and_tests(files)
