"""Freshness of the semantic memory.

Retrieval that returns last month's version of a function is worse than no
retrieval, because the model will trust it. Freshness is tracked per file by
git blob SHA; this module answers "how stale is the index?" without
re-embedding anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.data.ingestion import is_indexable


@dataclass
class Freshness:
    repo: str
    indexed_files: int
    stale: list[str]
    missing: list[str]
    orphaned: list[str]

    @property
    def fresh(self) -> bool:
        return not (self.stale or self.missing or self.orphaned)

    @property
    def stale_ratio(self) -> float:
        total = self.indexed_files + len(self.missing)
        return (len(self.stale) + len(self.missing)) / total if total else 0.0


async def check_freshness(store: Any, repo: str, tree: dict[str, str]) -> Freshness:
    """Compare the index with a tree (path -> blob SHA) of the default branch."""
    known = await store.get_file_index(repo)
    wanted = {p: sha for p, sha in tree.items() if is_indexable(p)}
    return Freshness(
        repo=repo,
        indexed_files=len(known),
        stale=sorted(p for p in wanted if p in known and known[p] != wanted[p]),
        missing=sorted(p for p in wanted if p not in known),
        orphaned=sorted(p for p in known if p not in wanted),
    )
