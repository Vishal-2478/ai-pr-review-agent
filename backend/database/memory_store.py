"""In-process implementation of the Store port.

Used by the offline demo (``make demo`` needs no database) and by unit tests.
It mirrors PostgresStore's observable behaviour, including the constraints
that matter for correctness: one review per commit, one pending approval per
review, compare-and-set on HITL resolution, and an append-only event log.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.agents.contracts import Finding
from backend.models.schemas import CodeChunk, Event, PRContext, RetrievedChunk

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_HISTORY = {"posted", "rejected", "disputed", "upheld", "retracted", "withheld"}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class InMemoryStore:
    def __init__(self) -> None:
        self.reviews: dict[str, dict[str, Any]] = {}
        self.findings: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.hitl: dict[str, dict[str, Any]] = {}
        self.feedback: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.chunks: dict[tuple[str, str], list[CodeChunk]] = {}
        self.file_index: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.conventions: dict[str, list[tuple[str, str]]] = defaultdict(list)

    # ------------------------------------------------------------------ reviews
    async def create_review(self, pr: PRContext) -> tuple[str, bool]:
        for rid, r in self.reviews.items():
            if (r["repo"], r["pr_number"], r["head_sha"]) == (pr.repo, pr.pr_number, pr.head_sha):
                return rid, False
        rid = str(uuid.uuid4())
        now = _iso(_now())
        self.reviews[rid] = {
            "id": rid, "repo": pr.repo, "pr_number": pr.pr_number, "head_sha": pr.head_sha,
            "base_sha": pr.base_sha, "title": pr.title[:500], "author": pr.author, "html_url": pr.html_url,
            "installation_id": pr.installation_id, "delivery_id": pr.delivery_id, "status": "queued",
            "decision": None, "overall_confidence": None, "coverage": None, "reasons": [], "summary": {},
            "cost_usd": 0.0, "github_review_id": None, "error": None, "created_at": now,
            "updated_at": now, "completed_at": None,
        }  # fmt: skip
        return rid, True

    async def get_review(self, review_id: str) -> dict[str, Any] | None:
        r = self.reviews.get(review_id)
        return dict(r) if r else None

    async def supersede_waiting(self, repo: str, pr_number: int, keep_review_id: str, reason: str) -> list[str]:
        ids = [
            r["id"] for r in self.reviews.values()
            if r["repo"] == repo and r["pr_number"] == pr_number and r["id"] != keep_review_id and r["status"] == "pending_human"
        ]  # fmt: skip
        for review_id in ids:
            r = self.reviews[review_id]
            r.update(status="superseded", completed_at=_iso(_now()), updated_at=_iso(_now()))
            r["reasons"] = [*(r.get("reasons") or []), reason]
            for h in self.hitl.values():
                if h["review_id"] == review_id and h["status"] == "pending" and h["kind"] == "approval":
                    h.update(status="expired", note=reason, resolved_at=_iso(_now()))
        return ids

    async def update_review(self, review_id: str, completed: bool = False, **fields: Any) -> None:
        r = self.reviews[review_id]
        r.update(fields)
        r["updated_at"] = _iso(_now())
        if completed:
            r["completed_at"] = _iso(_now())

    async def list_reviews(
        self, limit: int = 50, status: str | None = None, repo: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in self.reviews.values()
            if (status is None or r["status"] == status) and (repo is None or r["repo"] == repo)
        ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    async def save_findings(self, review_id: str, repo: str, pr_number: int, findings: list[Finding]) -> None:
        for f in findings:
            existing = self.findings[review_id].get(f.id, {})
            now = _iso(_now())
            self.findings[review_id][f.id] = {
                "review_id": review_id, "finding_id": f.id, "repo": repo, "pr_number": pr_number,
                "agent_type": f.agent_type.value, "severity": f.severity.value, "category": f.category,
                "summary": f.summary, "file_path": f.file_path, "line_start": f.line_start,
                "line_end": f.line_end, "suggestion": f.suggestion, "confidence": f.confidence,
                "raw_confidence": f.raw_confidence, "rationale": f.rationale, "evidence": f.evidence,
                "grounded": f.grounded, "in_diff": f.in_diff, "source": f.source,
                "corroborated_by": list(f.corroborated_by), "adjustments": list(f.adjustments),
                "status": f.status.value, "created_at": existing.get("created_at", now), "updated_at": now,
            }  # fmt: skip

    async def get_findings(self, review_id: str) -> list[dict[str, Any]]:
        rows = [dict(f) for f in self.findings.get(review_id, {}).values()]
        rows.sort(key=lambda f: (_SEV_ORDER.get(f["severity"], 9), -f["confidence"], f["finding_id"]))
        return rows

    async def set_finding_status(self, review_id: str, finding_ids: list[str], status: str) -> None:
        for fid in finding_ids:
            if fid in self.findings.get(review_id, {}):
                self.findings[review_id][fid]["status"] = status
                self.findings[review_id][fid]["updated_at"] = _iso(_now())

    async def find_finding(self, repo: str, pr_number: int, finding_id: str) -> dict[str, Any] | None:
        matches = [
            f
            for per_review in self.findings.values()
            for f in per_review.values()
            if (f["repo"], f["pr_number"], f["finding_id"]) == (repo, pr_number, finding_id)
        ]
        if not matches:
            return None
        row = dict(max(matches, key=lambda f: f["created_at"]))
        review = self.reviews[row["review_id"]]
        row.update(
            review_status=review["status"], head_sha=review["head_sha"], installation_id=review["installation_id"]
        )
        return row

    async def past_findings(
        self, repo: str, paths: list[str], exclude_review_id: str | None, limit: int = 12
    ) -> list[dict[str, Any]]:
        wanted = set(paths)
        rows = [
            {k: f[k] for k in ("file_path", "agent_type", "category", "severity", "summary", "status", "created_at")}
            for rid, per_review in self.findings.items()
            if rid != exclude_review_id
            for f in per_review.values()
            if f["repo"] == repo and f["file_path"] in wanted and f["status"] in _HISTORY
        ]
        rows.sort(key=lambda f: f["created_at"], reverse=True)
        return rows[:limit]

    # --------------------------------------------------------------------- HITL
    async def enqueue_hitl(
        self, review_id: str, kind: str, reason: str, priority: int,
        finding_id: str | None = None, requested_by: str = "",
    ) -> tuple[str, bool]:  # fmt: skip
        for hid, h in self.hitl.items():
            if h["review_id"] != review_id or h["status"] != "pending":
                continue
            if kind == "dispute" and h["kind"] == "dispute" and h["finding_id"] == finding_id:
                return hid, False
            if kind != "dispute" and h["kind"] != "dispute":
                return hid, False
        hid = str(uuid.uuid4())
        self.hitl[hid] = {
            "id": hid, "review_id": review_id, "kind": kind, "finding_id": finding_id, "status": "pending",
            "priority": priority, "reason": reason, "requested_by": requested_by, "reviewer": None,
            "note": None, "created_at": _iso(_now()), "resolved_at": None,
        }  # fmt: skip
        return hid, True

    def _hitl_view(self, h: dict[str, Any]) -> dict[str, Any]:
        r = self.reviews[h["review_id"]]
        pending = sum(1 for f in self.findings.get(h["review_id"], {}).values() if f["status"] == "pending_human")
        return {
            **h, "repo": r["repo"], "pr_number": r["pr_number"], "title": r["title"], "html_url": r["html_url"],
            "author": r["author"], "head_sha": r["head_sha"], "overall_confidence": r["overall_confidence"],
            "decision": r["decision"], "installation_id": r["installation_id"], "pending_findings": pending,
        }  # fmt: skip

    async def list_hitl(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        rows = [self._hitl_view(h) for h in self.hitl.values() if h["status"] == status]
        rows.sort(key=lambda h: (-h["priority"], h["created_at"]))
        return rows[:limit]

    async def get_hitl(self, hitl_id: str) -> dict[str, Any] | None:
        h = self.hitl.get(hitl_id)
        return self._hitl_view(h) if h else None

    async def resolve_hitl(self, hitl_id: str, status: str, reviewer: str, note: str) -> bool:
        h = self.hitl.get(hitl_id)
        if not h or h["status"] != "pending":
            return False
        h.update(status=status, reviewer=reviewer, note=note, resolved_at=_iso(_now()))
        return True

    async def hitl_stats(self) -> dict[str, Any]:
        pending = [h for h in self.hitl.values() if h["status"] == "pending"]
        oldest = min((datetime.fromisoformat(h["created_at"]) for h in pending), default=None)
        day_ago = _now() - timedelta(hours=24)
        return {
            "pending": len(pending),
            "oldest_pending_s": (_now() - oldest).total_seconds() if oldest else 0.0,
            "pending_escalations": sum(1 for h in pending if h["kind"] == "escalation"),
            "pending_disputes": sum(1 for h in pending if h["kind"] == "dispute"),
            "resolved_24h": sum(
                1
                for h in self.hitl.values()
                if h["resolved_at"] and datetime.fromisoformat(h["resolved_at"]) >= day_ago
            ),
        }

    async def add_feedback(
        self, *, review_id: str, finding_id: str, repo: str, agent_type: str, category: str,
        verdict: str, actor: str, source: str, note: str = "",
    ) -> None:  # fmt: skip
        self.feedback.append(
            {
                "review_id": review_id, "finding_id": finding_id, "repo": repo, "agent_type": agent_type,
                "category": category, "verdict": verdict, "actor": actor, "source": source, "note": note,
                "created_at": _iso(_now()),
            }  # fmt: skip
        )

    async def feedback_rows(self, repo: str, since_days: int = 180) -> list[dict[str, Any]]:
        cutoff = _now() - timedelta(days=since_days)
        rows = [
            {k: f[k] for k in ("agent_type", "category", "verdict", "actor", "created_at")}
            for f in self.feedback
            if f["repo"] == repo and datetime.fromisoformat(f["created_at"]) >= cutoff
        ]
        rows.sort(key=lambda f: f["created_at"], reverse=True)
        return rows

    # ------------------------------------------------------------------- events
    async def insert_events(self, events: list[Event]) -> None:
        for e in events:
            row = e.model_dump()
            row["ts"] = e.ts or _iso(_now())
            self.events.append(row)  # append-only: there is deliberately no update or delete

    async def get_trace(self, review_id: str) -> list[dict[str, Any]]:
        return sorted((dict(e) for e in self.events if e["review_id"] == review_id), key=lambda e: e["ts"])

    def _events_since(self, since: datetime) -> list[dict[str, Any]]:
        return [e for e in self.events if datetime.fromisoformat(e["ts"]) >= since]

    async def cost_today(self) -> float:
        midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        return float(sum(e["cost_usd"] for e in self._events_since(midnight)))

    async def review_cost(self, review_id: str) -> float:
        return float(sum(e["cost_usd"] for e in self.events if e["review_id"] == review_id))

    async def agent_health(self, hours: int = 24) -> list[dict[str, Any]]:
        by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in self._events_since(_now() - timedelta(hours=hours)):
            by_agent[e["agent"]].append(e)
        out = []
        for agent, evs in by_agent.items():
            lat = sorted(e["latency_ms"] for e in evs if e["event_type"] == "llm.call" and e["latency_ms"] is not None)
            p95 = float(lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)]) if lat else None
            fb = [e for e in evs if e["event_type"] == "feedback"]
            out.append(
                {
                    "agent": agent,
                    "llm_calls": sum(1 for e in evs if e["event_type"] == "llm.call"),
                    "cost_usd": float(sum(e["cost_usd"] for e in evs)),
                    "tokens_in": sum(e["tokens_in"] for e in evs),
                    "tokens_out": sum(e["tokens_out"] for e in evs),
                    "p95_latency_ms": p95,
                    "feedback_total": len(fb),
                    "feedback_negative": sum(1 for e in fb if e["outcome"] in ("rejected", "disputed")),
                    "errors": sum(1 for e in evs if e["event_type"] == "error"),
                }
            )
        return sorted(out, key=lambda r: (-r["cost_usd"], r["agent"]))

    async def cost_timeseries(self, hours: int = 48) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for e in self._events_since(_now() - timedelta(hours=hours)):
            t = datetime.fromisoformat(e["ts"]).replace(minute=0, second=0, microsecond=0).isoformat()
            b = buckets.setdefault(t, {"t": t, "cost_usd": 0.0, "llm_calls": 0})
            b["cost_usd"] += e["cost_usd"]
            b["llm_calls"] += 1 if e["event_type"] == "llm.call" else 0
        return [buckets[k] for k in sorted(buckets)]

    async def top_reviews_by_cost(self, hours: int = 168, limit: int = 10) -> list[dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        for e in self._events_since(_now() - timedelta(hours=hours)):
            rid = e["review_id"]
            if not rid or rid not in self.reviews:
                continue
            r = self.reviews[rid]
            t = totals.setdefault(
                rid,
                {"review_id": rid, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "repo": r["repo"],
                 "pr_number": r["pr_number"], "title": r["title"], "status": r["status"]},
            )  # fmt: skip
            t["cost_usd"] += e["cost_usd"]
            t["tokens_in"] += e["tokens_in"]
            t["tokens_out"] += e["tokens_out"]
        return sorted(totals.values(), key=lambda r: -r["cost_usd"])[:limit]

    async def decision_stats(self, hours: int = 24) -> dict[str, int]:
        cutoff = _now() - timedelta(hours=hours)
        out: dict[str, int] = defaultdict(int)
        for r in self.reviews.values():
            if r["decision"] and datetime.fromisoformat(r["created_at"]) >= cutoff:
                out[r["decision"]] += 1
        return dict(out)

    # ------------------------------------------------------------------- memory
    async def upsert_chunks(self, repo: str, path: str, chunks: list[CodeChunk]) -> None:
        stored = []
        for c in chunks:
            copy = c.model_copy()
            copy.id = copy.id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{repo}/{path}#{c.chunk_index}"))
            stored.append(copy)
        self.chunks[(repo, path)] = stored

    async def remove_file(self, repo: str, path: str) -> None:
        self.chunks.pop((repo, path), None)
        self.file_index[repo].pop(path, None)

    def _repo_chunks(self, repo: str) -> list[CodeChunk]:
        return [c for (r, _), cs in self.chunks.items() if r == repo for c in cs]

    @staticmethod
    def _retrieved(c: CodeChunk, **ranks: int) -> RetrievedChunk:
        return RetrievedChunk(
            id=c.id or "", path=c.path, symbol=c.symbol, start_line=c.start_line, end_line=c.end_line,
            content=c.content, token_count=c.token_count, **ranks,
        )  # fmt: skip

    async def search_vector(self, repo: str, embedding: list[float], k: int) -> list[RetrievedChunk]:
        def distance(c: CodeChunk) -> float:
            v = c.embedding or []
            dot = sum(a * b for a, b in zip(embedding, v, strict=False))
            na = math.sqrt(sum(a * a for a in embedding)) or 1.0
            nb = math.sqrt(sum(b * b for b in v)) or 1.0
            return 1.0 - dot / (na * nb)

        ranked = sorted(self._repo_chunks(repo), key=lambda c: (distance(c), c.path, c.chunk_index))[:k]
        return [self._retrieved(c, vector_rank=i + 1) for i, c in enumerate(ranked)]

    async def search_text(self, repo: str, term_groups: list[list[str]], k: int) -> list[RetrievedChunk]:
        import re

        scored: list[tuple[int, CodeChunk]] = []
        for c in self._repo_chunks(repo):
            tokens = re.findall(r"[a-z0-9]+", f"{c.symbol} {c.path} {c.content}".lower())
            counts: dict[str, int] = defaultdict(int)
            for t in tokens:
                counts[t] += 1
            score = sum(
                min(counts[t] for t in group) for group in term_groups if group and all(counts[t] for t in group)
            )
            if score:
                scored.append((score, c))
        scored.sort(key=lambda sc: (-sc[0], sc[1].path, sc[1].chunk_index))
        return [self._retrieved(c, text_rank=i + 1) for i, (_, c) in enumerate(scored[:k])]

    async def get_file_index(self, repo: str) -> dict[str, str]:
        return {path: row["blob_sha"] for path, row in self.file_index[repo].items()}

    async def set_file_indexed(self, repo: str, path: str, blob_sha: str, chunk_count: int) -> None:
        self.file_index[repo][path] = {"blob_sha": blob_sha, "chunk_count": chunk_count, "last_indexed_at": _iso(_now())}

    async def index_stats(self, repo: str) -> dict[str, Any]:
        rows = list(self.file_index[repo].values())
        return {
            "files": len(rows),
            "chunks": sum(r["chunk_count"] for r in rows),
            "last_indexed_at": max((r["last_indexed_at"] for r in rows), default=None),
        }

    async def get_conventions(self, repo: str, limit: int = 40) -> list[str]:
        return [rule for rule, _ in self.conventions[repo]][:limit]

    async def replace_conventions(self, repo: str, rules: list[str], source: str) -> None:
        kept = [(r, s) for r, s in self.conventions[repo] if s != source]
        seen = {r for r, _ in kept}
        for rule in rules:
            if rule not in seen:
                kept.append((rule, source))
                seen.add(rule)
        self.conventions[repo] = kept

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None
