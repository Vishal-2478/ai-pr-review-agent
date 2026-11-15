"""Context retrieval: what turns a stranger into a colleague.

A senior reviewer does not read a diff cold; they know the repository. The
agent gets the same footing from three memories, all in one store:

- semantic   : slices of the codebase relevant to this change (``code_chunks``)
- episodic   : what was said about these files before and how it landed
- procedural : the team's conventions

Semantic retrieval is hybrid. The diff is embedded and matched by vector
similarity (DiskANN) to find code that *means* something similar, and its
identifiers are matched by full-text search to find code that *names* the
same things. A function called ``charge_customer`` should be found by its
exact name even if the embedding ranks it low. The two rankings are merged
with reciprocal rank fusion.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from backend.economics.pricing import estimate_tokens
from backend.integrations.diff import parse_patch
from backend.memory.embedder import Embedder
from backend.models.schemas import DiffFile, PastFinding, RetrievedChunk, RetrievedContext
from backend.observability.events import EventEmitter

log = logging.getLogger(__name__)

RRF_K = 60
MAX_QUERY_FILES = 6
MAX_IDENTIFIERS = 14
MAX_CHUNKS_PER_FILE = 3
_STOP = {
    "self", "this", "none", "null", "true", "false", "return", "import", "from", "class", "def", "function",
    "const", "let", "var", "async", "await", "for", "while", "else", "elif", "except", "raise", "with",
    "print", "len", "str", "int", "dict", "list", "bool", "float", "string", "number", "object", "void",
    "public", "private", "static", "new", "try", "catch", "throw", "and", "not", "the", "args", "kwargs",
}  # fmt: skip


def extract_identifiers(files: list[DiffFile], limit: int = MAX_IDENTIFIERS) -> list[str]:
    """Identifiers the change touches, most telling first (things that are called or defined)."""
    scores: dict[str, float] = {}
    for f in files:
        parsed = parse_patch(f.patch)
        lines = list(parsed.added.values()) + [text for _, text in parsed.removed]
        for line in lines:
            for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{2,}", line):
                ident = m.group(0)
                if ident.lower() in _STOP or ident.isupper() and len(ident) < 4:
                    continue
                called = line[m.end() : m.end() + 1] == "("
                defined = bool(re.search(rf"\b(?:def|class|function|func)\s+{re.escape(ident)}\b", line))
                compound = "_" in ident.strip("_") or bool(re.search(r"[a-z][A-Z]", ident))
                scores[ident] = scores.get(ident, 0.0) + 1.0 + 2.0 * called + 3.0 * defined + 1.0 * compound
    ranked = sorted(scores, key=lambda i: (-scores[i], -len(i), i))
    return ranked[:limit]


def identifier_terms(identifier: str) -> list[str]:
    """Tokens as Postgres' ``simple`` text-search parser would index them.

    ``charge_customer`` is indexed as two lexemes (``charge``, ``customer``);
    ``chargeCustomer`` as one (``chargecustomer``).
    """
    return [t for t in re.split(r"[^a-z0-9]+", identifier.lower()) if t]


def reciprocal_rank_fusion(rankings: list[list[RetrievedChunk]], k: int = RRF_K) -> list[RetrievedChunk]:
    scores: dict[str, float] = {}
    best: dict[str, RetrievedChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (k + rank)
            kept = best.setdefault(chunk.id, chunk.model_copy())
            if chunk.vector_rank is not None:
                kept.vector_rank = min(kept.vector_rank or chunk.vector_rank, chunk.vector_rank)
            if chunk.text_rank is not None:
                kept.text_rank = min(kept.text_rank or chunk.text_rank, chunk.text_rank)
    fused = []
    for chunk_id in sorted(scores, key=lambda i: (-scores[i], i)):
        chunk = best[chunk_id]
        chunk.score = round(scores[chunk_id], 6)
        fused.append(chunk)
    return fused


class ContextRetriever:
    def __init__(self, store: Any, embedder: Embedder, emitter: EventEmitter) -> None:
        self._store = store
        self._embedder = embedder
        self._emitter = emitter

    async def retrieve(
        self,
        repo: str,
        files: list[DiffFile],
        *,
        review_id: str | None = None,
        top_k: int = 8,
        token_budget: int = 6000,
    ) -> RetrievedContext:
        async with self._emitter.span("retrieve_context", agent="retriever", review_id=review_id) as span:
            context = RetrievedContext()
            paths = [f.path for f in files]
            try:
                context.conventions = await self._store.get_conventions(repo)
                history = await self._store.past_findings(repo, paths, review_id)
                context.history = [PastFinding(**{**h, "created_at": str(h.get("created_at", ""))}) for h in history]
                stats = await self._store.index_stats(repo)
                context.indexed_files = int(stats.get("files") or 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("memory lookup failed: %s", exc)
                context.degraded, context.reason = True, f"memory store unavailable: {type(exc).__name__}"
                span.outcome = "degraded"
                span.set(reason=context.reason)
                return context

            if context.indexed_files == 0:
                context.degraded, context.reason = True, "repository has not been indexed yet"
                span.outcome = "degraded"
                span.set(reason=context.reason, conventions=len(context.conventions))
                return context

            try:
                context.chunks = await self._hybrid_search(repo, files, top_k, token_budget)
            except Exception as exc:  # noqa: BLE001 - retrieval failing must not fail the review
                log.warning("semantic retrieval failed: %s", exc)
                context.degraded, context.reason = True, f"retrieval failed: {type(exc).__name__}: {exc}"[:200]
                span.outcome = "degraded"
            span.set(
                chunks=[{"id": c.id, "path": c.path, "symbol": c.symbol, "score": c.score,
                         "vector_rank": c.vector_rank, "text_rank": c.text_rank} for c in context.chunks],
                conventions=len(context.conventions), history=len(context.history),
                indexed_files=context.indexed_files, reason=context.reason,
            )  # fmt: skip
            return context

    async def _hybrid_search(
        self, repo: str, files: list[DiffFile], top_k: int, token_budget: int
    ) -> list[RetrievedChunk]:
        queries = self._queries(files)
        identifiers = extract_identifiers(files)
        term_groups = [terms for terms in (identifier_terms(i) for i in identifiers) if terms]

        embeddings = await self._embedder.embed(queries) if queries else []
        # Both legs run in parallel; neither waits for the other.
        vector_task = asyncio.gather(*(self._store.search_vector(repo, e, top_k * 2) for e in embeddings))
        text_task = self._store.search_text(repo, term_groups, top_k * 2) if term_groups else _empty()
        vector_rankings, text_ranking = await asyncio.gather(vector_task, text_task)

        fused = reciprocal_rank_fusion([*vector_rankings, text_ranking])
        chosen: list[RetrievedChunk] = []
        per_file: dict[str, int] = {}
        used = 0
        for chunk in fused:
            if len(chosen) >= top_k:
                break
            if per_file.get(chunk.path, 0) >= MAX_CHUNKS_PER_FILE:
                continue
            cost = chunk.token_count or estimate_tokens(chunk.content)
            if used + cost > token_budget and chosen:
                continue
            chosen.append(chunk)
            per_file[chunk.path] = per_file.get(chunk.path, 0) + 1
            used += cost
        return chosen

    @staticmethod
    def _queries(files: list[DiffFile]) -> list[str]:
        """One query per changed file, biggest changes first: path + what was added and removed."""
        ranked = sorted((f for f in files if f.patch), key=lambda f: -(f.additions + f.deletions))
        queries = []
        for f in ranked[:MAX_QUERY_FILES]:
            parsed = parse_patch(f.patch)
            body = "\n".join(list(parsed.added.values()) + [text for _, text in parsed.removed])
            queries.append(f"{f.path}\n{body}"[:2000])
        return queries


async def _empty() -> list[RetrievedChunk]:
    return []
