"""Documentation specialist: will the next reader understand?"""

from __future__ import annotations

from backend.agents.base_agent import SpecialistAgent, source_and_docs
from backend.models.enums import AgentType, Severity
from backend.models.schemas import DiffFile


class DocsAgent(SpecialistAgent):
    agent_type = AgentType.DOCS
    max_severity = Severity.MEDIUM

    def relevant_files(self, files: list[DiffFile]) -> list[DiffFile]:
        return source_and_docs(files)
