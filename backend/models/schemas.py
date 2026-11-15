"""Plain data that crosses module boundaries (and, as dicts, the job queue)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PRContext(BaseModel):
    """Everything the workflow needs to know about the pull request under review."""

    repo: str  # "owner/name"
    pr_number: int
    head_sha: str
    base_sha: str = ""
    base_ref: str = ""
    title: str = ""
    body: str = ""
    author: str = ""
    html_url: str = ""
    installation_id: int | None = None
    delivery_id: str = ""
    draft: bool = False

    @property
    def key(self) -> str:
        """Natural idempotency key: one review per commit of a pull request."""
        return f"{self.repo}#{self.pr_number}@{self.head_sha}"


class DiffFile(BaseModel):
    path: str
    status: str = "modified"  # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0
    patch: str = ""
    previous_path: str | None = None


class CodeChunk(BaseModel):
    repo: str
    path: str
    chunk_index: int
    content: str
    symbol: str = ""
    start_line: int = 1
    end_line: int = 1
    token_count: int = 0
    embedding: list[float] | None = None
    id: str | None = None


class RetrievedChunk(BaseModel):
    id: str
    path: str
    symbol: str = ""
    start_line: int = 1
    end_line: int = 1
    content: str
    token_count: int = 0
    vector_rank: int | None = None
    text_rank: int | None = None
    score: float = 0.0


class PastFinding(BaseModel):
    """Episodic memory: what was said about these files before, and how it landed."""

    file_path: str
    agent_type: str
    category: str
    severity: str
    summary: str
    status: str
    created_at: str = ""


class RetrievedContext(BaseModel):
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    conventions: list[str] = Field(default_factory=list)
    history: list[PastFinding] = Field(default_factory=list)
    degraded: bool = False
    reason: str = ""
    indexed_files: int = 0

    def chunk_ids(self) -> list[str]:
        return [c.id for c in self.chunks]


class HitlDecisionItem(BaseModel):
    finding_id: str
    verdict: str  # accepted | rejected
    note: str = ""


class HitlDecisionRequest(BaseModel):
    action: str  # approve | reject
    reviewer: str = ""
    note: str = ""
    findings: list[HitlDecisionItem] = Field(default_factory=list)
    publish_withheld: bool = False


class FeedbackRequest(BaseModel):
    verdict: str  # accepted | rejected | disputed
    note: str = ""


class Event(BaseModel):
    """One row of the event spine."""

    review_id: str | None = None
    agent: str = "system"
    span_id: str = ""
    parent_span: str | None = None
    event_type: str
    name: str = ""
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int | None = None
    outcome: str | None = None
    confidence: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str | None = None
