"""Repository ingestion: build and refresh the semantic memory.

Indexing is incremental. ``repo_file_index`` remembers the git blob SHA of
every file as it was last embedded; a run re-embeds only files whose SHA
changed and removes files that are gone, so a push to main costs a handful of
embedding calls rather than a full re-index.

The same pass reads the team's written conventions (procedural memory) from
``.pr-review-agent.yml``, ``CONTRIBUTING.md`` and ADR titles.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.data.chunker import chunk_file
from backend.integrations.diff import SOURCE_SUFFIXES
from backend.memory.embedder import Embedder
from backend.observability.events import EventEmitter

log = logging.getLogger(__name__)

INDEXABLE_SUFFIXES = SOURCE_SUFFIXES + (".md", ".rst", ".yml", ".yaml", ".toml", ".json", ".graphql", ".proto")
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__", ".next", "target",
    ".mypy_cache", ".pytest_cache", "coverage", ".genesis", ".idea", ".vscode",
}  # fmt: skip
SKIP_SUFFIXES = (".min.js", ".lock", ".map", ".snap", ".svg", "package-lock.json", "pnpm-lock.yaml")
MAX_FILE_BYTES = 200_000
CONVENTION_FILES = (".pr-review-agent.yml", ".pr-review-agent.yaml", "CONTRIBUTING.md", ".github/CONTRIBUTING.md")
MAX_CONVENTIONS = 40


@dataclass
class IndexReport:
    repo: str
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    chunks: int = 0
    conventions: int = 0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324 - git's own object id


def is_indexable(path: str, size: int | None = None) -> bool:
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    lower = path.lower()
    if lower.endswith(SKIP_SUFFIXES) or not lower.endswith(INDEXABLE_SUFFIXES):
        return False
    return size is None or size <= MAX_FILE_BYTES


def parse_conventions(files: dict[str, str]) -> list[str]:
    """Pull short, rule-like statements out of the team's own documents."""
    rules: list[str] = []
    for name in (".pr-review-agent.yml", ".pr-review-agent.yaml"):
        if name in files:
            try:
                data = yaml.safe_load(files[name]) or {}
                rules.extend(str(r).strip() for r in data.get("conventions", []) if str(r).strip())
            except yaml.YAMLError as exc:
                log.warning("could not parse %s: %s", name, exc)
    for name, text in files.items():
        if name.lower().endswith("contributing.md"):
            for line in text.splitlines():
                m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.{15,220})$", line)
                if m and re.search(r"(?i)\b(must|should|never|always|do not|don't|prefer|avoid|require)", m.group(1)):
                    rules.append(m.group(1).strip())
        elif "/adr/" in f"/{name.lower()}" and name.lower().endswith(".md"):
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), "")
            if title:
                rules.append(f"Architecture decision: {title}")
    seen: set[str] = set()
    unique = [r for r in rules if not (r in seen or seen.add(r))]  # type: ignore[func-returns-value]
    return unique[:MAX_CONVENTIONS]


class RepoIndexer:
    def __init__(self, store: Any, embedder: Embedder, emitter: EventEmitter, max_files: int = 3000) -> None:
        self._store = store
        self._embedder = embedder
        self._emitter = emitter
        self._max_files = max_files

    async def index(
        self,
        repo: str,
        tree: dict[str, str],
        load: Callable[[str], Awaitable[str | None]],
        *,
        only_paths: set[str] | None = None,
    ) -> IndexReport:
        """Bring the index in line with ``tree`` (path -> blob SHA at the default branch)."""
        report = IndexReport(repo=repo)
        async with self._emitter.span("index_repository", agent="indexer", repo=repo) as span:
            known = await self._store.get_file_index(repo)
            wanted = {p: sha for p, sha in tree.items() if is_indexable(p)}
            report.skipped = len(tree) - len(wanted)

            for path in sorted(set(known) - set(wanted)):
                if only_paths is None or path in only_paths:
                    await self._store.remove_file(repo, path)
                    report.removed += 1

            todo = [p for p in sorted(wanted) if known.get(p) != wanted[p] and (only_paths is None or p in only_paths)]
            report.unchanged = len(wanted) - len(todo)
            if len(todo) > self._max_files:
                report.truncated = True
                todo = todo[: self._max_files]

            for path in todo:
                try:
                    content = await load(path)
                    if content is None:
                        report.skipped += 1
                        continue
                    chunks = chunk_file(repo, path, content)
                    if chunks:
                        vectors = await self._embedder.embed([f"{path}\n{c.symbol}\n{c.content}" for c in chunks])
                        for chunk, vector in zip(chunks, vectors, strict=True):
                            chunk.embedding = vector
                        await self._store.upsert_chunks(repo, path, chunks)
                    else:
                        await self._store.upsert_chunks(repo, path, [])
                    await self._store.set_file_indexed(repo, path, wanted[path], len(chunks))
                    report.indexed += 1
                    report.chunks += len(chunks)
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                    report.errors.append(f"{path}: {type(exc).__name__}: {exc}"[:200])

            convention_sources: dict[str, str] = {}
            for path in tree:
                if path in CONVENTION_FILES or ("/adr/" in f"/{path.lower()}" and path.lower().endswith(".md")):
                    if len(convention_sources) < 30:
                        text = await load(path)
                        if text:
                            convention_sources[path] = text
            rules = parse_conventions(convention_sources)
            if rules or only_paths is None:
                await self._store.replace_conventions(repo, rules, "repo")
            report.conventions = len(rules)
            span.set(**{k: v for k, v in report.__dict__.items() if k != "errors"}, errors=report.errors[:10])
            if report.errors:
                span.outcome = "degraded"
        return report

    async def index_local(self, repo: str, root: Path) -> IndexReport:
        """Index a checkout on disk (CLI, demo, tests)."""
        root = root.resolve()
        tree: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            tree[rel] = git_blob_sha(path.read_bytes())

        async def load(rel: str) -> str | None:
            try:
                return (root / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                return None

        return await self.index(repo, tree, load)

    async def index_github(
        self, repo: str, github: Any, ref: str, installation_id: int | None, only_paths: set[str] | None = None
    ) -> IndexReport:
        """Index a repository through the GitHub API (no clone, no code execution)."""
        entries = await github.get_tree(repo, ref, installation_id)
        tree = {e["path"]: e["sha"] for e in entries if e.get("type") == "blob" and (e.get("size") or 0) <= MAX_FILE_BYTES}

        async def load(path: str) -> str | None:
            return await github.get_blob_text(repo, tree[path], installation_id)

        return await self.index(repo, tree, load, only_paths=only_paths)
