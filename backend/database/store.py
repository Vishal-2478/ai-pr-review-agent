"""Persistence port and its Postgres/Tiger implementation.

``Store`` is the only way application code touches durable state. Two
implementations satisfy it: :class:`PostgresStore` (this file) and
:class:`backend.database.memory_store.InMemoryStore` (offline demo and unit
tests). ``tests/test_store_contract.py`` runs one suite against both so they
cannot drift apart.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from backend.agents.contracts import Finding
from backend.database.pool import normalize_row, vector_literal
from backend.models.schemas import CodeChunk, Event, PRContext, RetrievedChunk

_REVIEW_COLUMNS = {
    "status", "decision", "overall_confidence", "coverage", "reasons", "summary",
    "cost_usd", "github_review_id", "error", "title", "installation_id",
}  # fmt: skip

_FINDING_STATUS_FOR_HISTORY = ("posted", "rejected", "disputed", "upheld", "retracted", "withheld")


class Store(Protocol):
    # ---- truth: reviews & findings
    async def create_review(self, pr: PRContext) -> tuple[str, bool]: ...
    async def get_review(self, review_id: str) -> dict[str, Any] | None: ...
    async def supersede_waiting(self, repo: str, pr_number: int, keep_review_id: str, reason: str) -> list[str]: ...
    async def update_review(self, review_id: str, completed: bool = False, **fields: Any) -> None: ...
    async def list_reviews(self, limit: int = 50, status: str | None = None, repo: str | None = None) -> list[dict[str, Any]]: ...
    async def save_findings(self, review_id: str, repo: str, pr_number: int, findings: list[Finding]) -> None: ...
    async def get_findings(self, review_id: str) -> list[dict[str, Any]]: ...
    async def set_finding_status(self, review_id: str, finding_ids: list[str], status: str) -> None: ...
    async def find_finding(self, repo: str, pr_number: int, finding_id: str) -> dict[str, Any] | None: ...
    async def past_findings(self, repo: str, paths: list[str], exclude_review_id: str | None, limit: int = 12) -> list[dict[str, Any]]: ...

    # ---- truth: human in the loop
    async def enqueue_hitl(self, review_id: str, kind: str, reason: str, priority: int, finding_id: str | None = None, requested_by: str = "") -> tuple[str, bool]: ...
    async def list_hitl(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]: ...
    async def get_hitl(self, hitl_id: str) -> dict[str, Any] | None: ...
    async def resolve_hitl(self, hitl_id: str, status: str, reviewer: str, note: str) -> bool: ...
    async def hitl_stats(self) -> dict[str, Any]: ...
    async def add_feedback(self, *, review_id: str, finding_id: str, repo: str, agent_type: str, category: str, verdict: str, actor: str, source: str, note: str = "") -> None: ...
    async def feedback_rows(self, repo: str, since_days: int = 180) -> list[dict[str, Any]]: ...

    # ---- time: the event spine and its rollups
    async def insert_events(self, events: list[Event]) -> None: ...
    async def get_trace(self, review_id: str) -> list[dict[str, Any]]: ...
    async def cost_today(self) -> float: ...
    async def review_cost(self, review_id: str) -> float: ...
    async def agent_health(self, hours: int = 24) -> list[dict[str, Any]]: ...
    async def cost_timeseries(self, hours: int = 48) -> list[dict[str, Any]]: ...
    async def top_reviews_by_cost(self, hours: int = 168, limit: int = 10) -> list[dict[str, Any]]: ...
    async def decision_stats(self, hours: int = 24) -> dict[str, int]: ...

    # ---- memory: semantic + procedural
    async def upsert_chunks(self, repo: str, path: str, chunks: list[CodeChunk]) -> None: ...
    async def remove_file(self, repo: str, path: str) -> None: ...
    async def search_vector(self, repo: str, embedding: list[float], k: int) -> list[RetrievedChunk]: ...
    async def search_text(self, repo: str, term_groups: list[list[str]], k: int) -> list[RetrievedChunk]: ...
    async def get_file_index(self, repo: str) -> dict[str, str]: ...
    async def set_file_indexed(self, repo: str, path: str, blob_sha: str, chunk_count: int) -> None: ...
    async def index_stats(self, repo: str) -> dict[str, Any]: ...
    async def get_conventions(self, repo: str, limit: int = 40) -> list[str]: ...
    async def replace_conventions(self, repo: str, rules: list[str], source: str) -> None: ...

    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


def tsquery_from_groups(term_groups: list[list[str]]) -> str:
    """[['charge','customer'],['retry']] -> "(charge & customer) | (retry)"."""
    parts = []
    for group in term_groups:
        tokens = [t for t in group if t.isalnum()]
        if tokens:
            parts.append("(" + " & ".join(tokens) + ")")
    return " | ".join(parts)


def _chunk(row: Any, *, vector_rank: int | None = None, text_rank: int | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        id=str(row["id"]),
        path=row["path"],
        symbol=row["symbol"] or "",
        start_line=row["start_line"],
        end_line=row["end_line"],
        content=row["content"],
        token_count=row["token_count"],
        vector_rank=vector_rank,
        text_rank=text_rank,
    )


class PostgresStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ------------------------------------------------------------------ reviews
    async def create_review(self, pr: PRContext) -> tuple[str, bool]:
        review_id = str(uuid.uuid4())
        row = await self.pool.fetchrow(
            """
            INSERT INTO pr_review_records
                (id, repo, pr_number, head_sha, base_sha, title, author, html_url, installation_id, delivery_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT ON CONSTRAINT pr_review_one_per_commit DO NOTHING
            RETURNING id
            """,
            uuid.UUID(review_id), pr.repo, pr.pr_number, pr.head_sha, pr.base_sha, pr.title[:500],
            pr.author, pr.html_url, pr.installation_id, pr.delivery_id,
        )  # fmt: skip
        if row is not None:
            return review_id, True
        existing = await self.pool.fetchval(
            "SELECT id FROM pr_review_records WHERE repo = $1 AND pr_number = $2 AND head_sha = $3",
            pr.repo, pr.pr_number, pr.head_sha,
        )  # fmt: skip
        return str(existing), False

    async def get_review(self, review_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM pr_review_records WHERE id = $1", uuid.UUID(review_id))
        return normalize_row(row) if row else None

    async def supersede_waiting(self, repo: str, pr_number: int, keep_review_id: str, reason: str) -> list[str]:
        """A newer commit arrived: reviews of older commits still waiting for approval are moot.

        Their approval items expire so nobody approves comments onto a stale
        commit. Escalations are left alone on purpose: a leaked credential is
        still leaked after the next push, and a human must still look.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE pr_review_records
                   SET status = 'superseded', completed_at = now(), updated_at = now(),
                       reasons = reasons || to_jsonb($4::text)
                 WHERE repo = $1 AND pr_number = $2 AND id <> $3 AND status = 'pending_human'
             RETURNING id
                """,
                repo, pr_number, uuid.UUID(keep_review_id), reason,
            )  # fmt: skip
            ids = [r["id"] for r in rows]
            if ids:
                await conn.execute(
                    "UPDATE hitl_reviews SET status = 'expired', note = $2, resolved_at = now() "
                    "WHERE review_id = ANY($1::uuid[]) AND status = 'pending' AND kind = 'approval'",
                    ids, reason,
                )  # fmt: skip
        return [str(i) for i in ids]

    async def update_review(self, review_id: str, completed: bool = False, **fields: Any) -> None:
        unknown = set(fields) - _REVIEW_COLUMNS
        if unknown:
            raise ValueError(f"unknown review columns: {sorted(unknown)}")
        sets = [f"{col} = ${i + 2}" for i, col in enumerate(fields)]
        sets.append("updated_at = now()")
        if completed:
            sets.append("completed_at = now()")
        # Column names come from the whitelist above, never from callers' data.
        sql = f"UPDATE pr_review_records SET {', '.join(sets)} WHERE id = $1"  # noqa: S608
        await self.pool.execute(sql, uuid.UUID(review_id), *fields.values())

    async def list_reviews(
        self, limit: int = 50, status: str | None = None, repo: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM pr_review_records
            WHERE ($1::text IS NULL OR status = $1) AND ($2::text IS NULL OR repo = $2)
            ORDER BY created_at DESC LIMIT $3
            """,
            status, repo, limit,
        )  # fmt: skip
        return [normalize_row(r) for r in rows]

    async def save_findings(self, review_id: str, repo: str, pr_number: int, findings: list[Finding]) -> None:
        if not findings:
            return
        rid = uuid.UUID(review_id)
        await self.pool.executemany(
            """
            INSERT INTO finding_records
                (review_id, finding_id, repo, pr_number, agent_type, severity, category, summary, file_path,
                 line_start, line_end, suggestion, confidence, raw_confidence, rationale, evidence, grounded,
                 in_diff, source, corroborated_by, adjustments, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
            ON CONFLICT (review_id, finding_id) DO UPDATE SET
                severity = EXCLUDED.severity, confidence = EXCLUDED.confidence,
                raw_confidence = EXCLUDED.raw_confidence, grounded = EXCLUDED.grounded,
                in_diff = EXCLUDED.in_diff, corroborated_by = EXCLUDED.corroborated_by,
                adjustments = EXCLUDED.adjustments, status = EXCLUDED.status, updated_at = now()
            """,
            [
                (
                    rid, f.id, repo, pr_number, f.agent_type.value, f.severity.value, f.category, f.summary,
                    f.file_path, f.line_start, f.line_end, f.suggestion, f.confidence, f.raw_confidence,
                    f.rationale, f.evidence, f.grounded, f.in_diff, f.source, f.corroborated_by,
                    f.adjustments, f.status.value,
                )
                for f in findings
            ],
        )  # fmt: skip

    async def get_findings(self, review_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM finding_records WHERE review_id = $1
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                                   WHEN 'low' THEN 3 ELSE 4 END, confidence DESC, finding_id
            """,
            uuid.UUID(review_id),
        )
        return [normalize_row(r) for r in rows]

    async def set_finding_status(self, review_id: str, finding_ids: list[str], status: str) -> None:
        if not finding_ids:
            return
        await self.pool.execute(
            "UPDATE finding_records SET status = $3, updated_at = now() "
            "WHERE review_id = $1 AND finding_id = ANY($2::text[])",
            uuid.UUID(review_id), finding_ids, status,
        )  # fmt: skip

    async def find_finding(self, repo: str, pr_number: int, finding_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT f.*, r.status AS review_status, r.head_sha, r.installation_id
            FROM finding_records f JOIN pr_review_records r ON r.id = f.review_id
            WHERE f.repo = $1 AND f.pr_number = $2 AND f.finding_id = $3
            ORDER BY f.created_at DESC LIMIT 1
            """,
            repo, pr_number, finding_id,
        )  # fmt: skip
        return normalize_row(row) if row else None

    async def past_findings(
        self, repo: str, paths: list[str], exclude_review_id: str | None, limit: int = 12
    ) -> list[dict[str, Any]]:
        if not paths:
            return []
        rows = await self.pool.fetch(
            """
            SELECT file_path, agent_type, category, severity, summary, status, created_at
            FROM finding_records
            WHERE repo = $1 AND file_path = ANY($2::text[]) AND status = ANY($3::text[])
              AND ($4::uuid IS NULL OR review_id <> $4)
            ORDER BY created_at DESC LIMIT $5
            """,
            repo, paths, list(_FINDING_STATUS_FOR_HISTORY),
            uuid.UUID(exclude_review_id) if exclude_review_id else None, limit,
        )  # fmt: skip
        return [normalize_row(r) for r in rows]

    # --------------------------------------------------------------------- HITL
    async def enqueue_hitl(
        self, review_id: str, kind: str, reason: str, priority: int,
        finding_id: str | None = None, requested_by: str = "",
    ) -> tuple[str, bool]:  # fmt: skip
        hitl_id = str(uuid.uuid4())
        try:
            await self.pool.execute(
                "INSERT INTO hitl_reviews (id, review_id, kind, finding_id, reason, priority, requested_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                uuid.UUID(hitl_id), uuid.UUID(review_id), kind, finding_id, reason, priority, requested_by,
            )  # fmt: skip
            return hitl_id, True
        except asyncpg.UniqueViolationError:
            existing = await self.pool.fetchval(
                """
                SELECT id FROM hitl_reviews WHERE review_id = $1 AND status = 'pending'
                  AND ((kind = 'dispute' AND $2 = 'dispute' AND finding_id = $3)
                       OR (kind <> 'dispute' AND $2 <> 'dispute'))
                LIMIT 1
                """,
                uuid.UUID(review_id), kind, finding_id,
            )  # fmt: skip
            return str(existing), False

    async def list_hitl(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT h.*, r.repo, r.pr_number, r.title, r.html_url, r.author, r.head_sha,
                   r.overall_confidence, r.decision,
                   (SELECT count(*) FROM finding_records f
                     WHERE f.review_id = h.review_id AND f.status = 'pending_human') AS pending_findings
            FROM hitl_reviews h JOIN pr_review_records r ON r.id = h.review_id
            WHERE h.status = $1
            ORDER BY h.priority DESC, h.created_at ASC LIMIT $2
            """,
            status, limit,
        )  # fmt: skip
        return [normalize_row(r) for r in rows]

    async def get_hitl(self, hitl_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT h.*, r.repo, r.pr_number, r.title, r.html_url, r.author, r.head_sha,
                   r.overall_confidence, r.decision, r.installation_id
            FROM hitl_reviews h JOIN pr_review_records r ON r.id = h.review_id WHERE h.id = $1
            """,
            uuid.UUID(hitl_id),
        )
        return normalize_row(row) if row else None

    async def resolve_hitl(self, hitl_id: str, status: str, reviewer: str, note: str) -> bool:
        # The status guard makes this a compare-and-set: two reviewers clicking
        # at once cannot both win, so a review is never published twice.
        row = await self.pool.fetchrow(
            "UPDATE hitl_reviews SET status = $2, reviewer = $3, note = $4, resolved_at = now() "
            "WHERE id = $1 AND status = 'pending' RETURNING id",
            uuid.UUID(hitl_id), status, reviewer, note,
        )  # fmt: skip
        return row is not None

    async def hitl_stats(self) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT count(*) FILTER (WHERE status = 'pending')                               AS pending,
                   coalesce(extract(epoch FROM now() - min(created_at)
                            FILTER (WHERE status = 'pending')), 0)::float8                  AS oldest_pending_s,
                   count(*) FILTER (WHERE status = 'pending' AND kind = 'escalation')       AS pending_escalations,
                   count(*) FILTER (WHERE status = 'pending' AND kind = 'dispute')          AS pending_disputes,
                   count(*) FILTER (WHERE resolved_at >= now() - INTERVAL '24 hours')       AS resolved_24h
            FROM hitl_reviews
            """
        )
        return normalize_row(row)

    async def add_feedback(
        self, *, review_id: str, finding_id: str, repo: str, agent_type: str, category: str,
        verdict: str, actor: str, source: str, note: str = "",
    ) -> None:  # fmt: skip
        await self.pool.execute(
            "INSERT INTO hitl_feedback (review_id, finding_id, repo, agent_type, category, verdict, actor, source, note) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            uuid.UUID(review_id), finding_id, repo, agent_type, category, verdict, actor, source, note,
        )  # fmt: skip

    async def feedback_rows(self, repo: str, since_days: int = 180) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT agent_type, category, verdict, actor, created_at FROM hitl_feedback "
            "WHERE repo = $1 AND created_at >= now() - make_interval(days => $2) "
            "ORDER BY created_at DESC LIMIT 5000",
            repo, since_days,
        )  # fmt: skip
        return [normalize_row(r) for r in rows]

    # ------------------------------------------------------------------- events
    async def insert_events(self, events: list[Event]) -> None:
        if not events:
            return
        await self.pool.executemany(
            """
            INSERT INTO agent_events
                (ts, review_id, agent, span_id, parent_span, event_type, name, model, tokens_in, tokens_out,
                 cost_usd, latency_ms, outcome, confidence, payload)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """,
            [
                (
                    datetime.fromisoformat(e.ts) if e.ts else datetime.now(UTC),
                    uuid.UUID(e.review_id) if e.review_id else None,
                    e.agent, e.span_id, e.parent_span, e.event_type, e.name, e.model, e.tokens_in,
                    e.tokens_out, e.cost_usd, e.latency_ms, e.outcome, e.confidence, e.payload,
                )
                for e in events
            ],
        )  # fmt: skip

    async def get_trace(self, review_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM agent_events WHERE review_id = $1 ORDER BY ts", uuid.UUID(review_id)
        )
        return [normalize_row(r) for r in rows]

    async def cost_today(self) -> float:
        # Reads the continuous aggregate, not the raw hypertable (ADR-004).
        value = await self.pool.fetchval(
            "SELECT coalesce(sum(cost_usd), 0) FROM agent_health_1m WHERE bucket >= date_trunc('day', now())"
        )
        return float(value or 0.0)

    async def review_cost(self, review_id: str) -> float:
        value = await self.pool.fetchval(
            "SELECT coalesce(sum(cost_usd), 0) FROM agent_events WHERE review_id = $1", uuid.UUID(review_id)
        )
        return float(value or 0.0)

    async def agent_health(self, hours: int = 24) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT agent,
                   coalesce(sum(llm_calls), 0)::bigint         AS llm_calls,
                   coalesce(sum(cost_usd), 0)::float8          AS cost_usd,
                   coalesce(sum(tokens_in), 0)::bigint         AS tokens_in,
                   coalesce(sum(tokens_out), 0)::bigint        AS tokens_out,
                   max(p95_latency_ms)::float8                 AS p95_latency_ms,
                   coalesce(sum(feedback_total), 0)::bigint    AS feedback_total,
                   coalesce(sum(feedback_negative), 0)::bigint AS feedback_negative,
                   coalesce(sum(errors), 0)::bigint            AS errors
            FROM agent_health_1m
            WHERE bucket >= now() - make_interval(hours => $1)
            GROUP BY agent ORDER BY 3 DESC, agent
            """,
            hours,
        )
        return [normalize_row(r) for r in rows]

    async def cost_timeseries(self, hours: int = 48) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT date_trunc('hour', bucket)           AS t,
                   coalesce(sum(cost_usd), 0)::float8   AS cost_usd,
                   coalesce(sum(llm_calls), 0)::bigint  AS llm_calls
            FROM agent_health_1m
            WHERE bucket >= now() - make_interval(hours => $1)
            GROUP BY 1 ORDER BY 1
            """,
            hours,
        )
        return [normalize_row(r) for r in rows]

    async def top_reviews_by_cost(self, hours: int = 168, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT c.review_id, coalesce(sum(c.cost_usd), 0)::float8 AS cost_usd,
                   coalesce(sum(c.tokens_in), 0)::bigint AS tokens_in,
                   coalesce(sum(c.tokens_out), 0)::bigint AS tokens_out,
                   r.repo, r.pr_number, r.title, r.status
            FROM pr_cost_hourly c JOIN pr_review_records r ON r.id = c.review_id
            WHERE c.bucket >= now() - make_interval(hours => $1)
            GROUP BY c.review_id, r.repo, r.pr_number, r.title, r.status
            ORDER BY 2 DESC LIMIT $2
            """,
            hours, limit,
        )  # fmt: skip
        return [normalize_row(r) for r in rows]

    async def decision_stats(self, hours: int = 24) -> dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT decision, count(*) AS n FROM pr_review_records "
            "WHERE decision IS NOT NULL AND created_at >= now() - make_interval(hours => $1) GROUP BY decision",
            hours,
        )
        return {r["decision"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------- memory
    async def upsert_chunks(self, repo: str, path: str, chunks: list[CodeChunk]) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            # A file that shrank must not leave its old tail behind.
            await conn.execute(
                "DELETE FROM code_chunks WHERE repo = $1 AND path = $2 AND chunk_index >= $3",
                repo, path, len(chunks),
            )  # fmt: skip
            if not chunks:
                return
            await conn.executemany(
                """
                INSERT INTO code_chunks
                    (repo, path, symbol, chunk_index, start_line, end_line, content, embedding, token_count)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector, $9)
                ON CONFLICT ON CONSTRAINT code_chunks_upsert_key DO UPDATE SET
                    symbol = EXCLUDED.symbol, start_line = EXCLUDED.start_line, end_line = EXCLUDED.end_line,
                    content = EXCLUDED.content, embedding = EXCLUDED.embedding,
                    token_count = EXCLUDED.token_count, updated_at = now()
                """,
                [
                    (
                        repo, path, c.symbol, c.chunk_index, c.start_line, c.end_line, c.content,
                        vector_literal(c.embedding or []), c.token_count,
                    )
                    for c in chunks
                ],
            )  # fmt: skip

    async def remove_file(self, repo: str, path: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM code_chunks WHERE repo = $1 AND path = $2", repo, path)
            await conn.execute("DELETE FROM repo_file_index WHERE repo = $1 AND path = $2", repo, path)

    async def search_vector(self, repo: str, embedding: list[float], k: int) -> list[RetrievedChunk]:
        rows = await self.pool.fetch(
            """
            SELECT id, path, symbol, start_line, end_line, content, token_count
            FROM code_chunks WHERE repo = $1
            ORDER BY embedding <=> $2::vector LIMIT $3
            """,
            repo, vector_literal(embedding), k,
        )  # fmt: skip
        return [_chunk(r, vector_rank=i + 1) for i, r in enumerate(rows)]

    async def search_text(self, repo: str, term_groups: list[list[str]], k: int) -> list[RetrievedChunk]:
        query = tsquery_from_groups(term_groups)
        if not query:
            return []
        rows = await self.pool.fetch(
            """
            SELECT c.id, c.path, c.symbol, c.start_line, c.end_line, c.content, c.token_count
            FROM code_chunks c, to_tsquery('simple', $2) q
            WHERE c.repo = $1 AND c.content_tsv @@ q
            ORDER BY ts_rank_cd(c.content_tsv, q) DESC, c.path, c.chunk_index LIMIT $3
            """,
            repo, query, k,
        )  # fmt: skip
        return [_chunk(r, text_rank=i + 1) for i, r in enumerate(rows)]

    async def get_file_index(self, repo: str) -> dict[str, str]:
        rows = await self.pool.fetch("SELECT path, blob_sha FROM repo_file_index WHERE repo = $1", repo)
        return {r["path"]: r["blob_sha"] for r in rows}

    async def set_file_indexed(self, repo: str, path: str, blob_sha: str, chunk_count: int) -> None:
        await self.pool.execute(
            """
            INSERT INTO repo_file_index (repo, path, blob_sha, chunk_count) VALUES ($1, $2, $3, $4)
            ON CONFLICT (repo, path) DO UPDATE SET
                blob_sha = EXCLUDED.blob_sha, chunk_count = EXCLUDED.chunk_count, last_indexed_at = now()
            """,
            repo, path, blob_sha, chunk_count,
        )  # fmt: skip

    async def index_stats(self, repo: str) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            "SELECT count(*) AS files, coalesce(sum(chunk_count), 0)::bigint AS chunks, "
            "max(last_indexed_at) AS last_indexed_at FROM repo_file_index WHERE repo = $1",
            repo,
        )
        return normalize_row(row)

    async def get_conventions(self, repo: str, limit: int = 40) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT rule FROM repo_conventions WHERE repo = $1 ORDER BY id LIMIT $2", repo, limit
        )
        return [r["rule"] for r in rows]

    async def replace_conventions(self, repo: str, rules: list[str], source: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM repo_conventions WHERE repo = $1 AND source = $2", repo, source)
            await conn.executemany(
                "INSERT INTO repo_conventions (repo, rule, source) VALUES ($1, $2, $3) "
                "ON CONFLICT ON CONSTRAINT repo_conventions_unique DO NOTHING",
                [(repo, rule, source) for rule in rules],
            )

    async def ping(self) -> bool:
        return await self.pool.fetchval("SELECT 1") == 1

    async def close(self) -> None:
        await self.pool.close()
