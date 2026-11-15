"""Versioned prompt registry.

Prompts are code. Each one lives in ``templates/<name>.v<N>.md``; a new
version is a new file, never an edit in place. The version and a content hash
are recorded on every ``llm.call`` event, so when a finding is disputed you
can tell exactly which words produced it, and an evaluation run can compare
v1 against v2 on the same golden set.

Pin a version with PROMPT_VERSION_<NAME>=<N>; otherwise the highest is used.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

TEMPLATES_DIR = Path(__file__).with_name("templates")
_FILE = re.compile(r"^(?P<name>[a-z_]+)\.v(?P<version>\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:12]

    @property
    def ref(self) -> str:
        return f"{self.name}.v{self.version}@{self.sha}"


class PromptRegistry:
    def __init__(self, directory: Path = TEMPLATES_DIR) -> None:
        self._prompts: dict[str, dict[int, Prompt]] = {}
        for path in sorted(directory.glob("*.md")):
            m = _FILE.match(path.name)
            if not m:
                continue
            name, version = m.group("name"), int(m.group("version"))
            self._prompts.setdefault(name, {})[version] = Prompt(name, version, path.read_text().strip())

    def versions(self, name: str) -> list[int]:
        return sorted(self._prompts.get(name, {}))

    def get(self, name: str, version: int | None = None) -> Prompt:
        available = self._prompts.get(name)
        if not available:
            raise KeyError(f"no prompt named '{name}'")
        if version is None:
            pinned = os.environ.get(f"PROMPT_VERSION_{name.upper()}")
            version = int(pinned) if pinned else max(available)
        if version not in available:
            raise KeyError(f"prompt '{name}' has no version {version} (have {sorted(available)})")
        return available[version]

    def system_for(self, agent: str) -> tuple[str, str]:
        """Shared contract + the agent's own concern. Returns (text, reference)."""
        base, own = self.get("base"), self.get(agent)
        return f"{base.text}\n\n{own.text}", f"{base.ref}+{own.ref}"


@lru_cache
def default_registry() -> PromptRegistry:
    return PromptRegistry()
