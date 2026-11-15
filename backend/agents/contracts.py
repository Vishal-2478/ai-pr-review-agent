"""The Finding: the unit that flows through the whole system.

A review is not a blob of prose. It is a list of findings, and every finding
says *who* raised it, *how bad* it is, *where* it is, *why* the agent believes
it, and *how sure* it is. That shape is what makes a finding mergeable,
auditable, disputable and gateable.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.models.enums import AgentType, FindingStatus, GateDecision, Severity


def finding_id(agent: str, file_path: str, line_start: int, category: str, summary: str) -> str:
    """Stable id, so a retried run or a second webhook produces the same finding."""
    norm = re.sub(r"\s+", " ", summary.strip().lower())
    digest = hashlib.sha1(f"{agent}|{file_path}|{line_start}|{category}|{norm}".encode()).hexdigest()  # noqa: S324
    return f"F-{digest[:8]}"


class LLMFinding(BaseModel):
    """What a specialist model is asked to return. Deliberately forgiving on input."""

    severity: str
    category: str
    summary: str
    file_path: str
    line_start: int
    line_end: int | None = None
    suggestion: str = ""
    confidence: float
    rationale: str
    evidence: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> float:
        if isinstance(v, str):
            v = v.strip().rstrip("%")
            v = float(v)
        v = float(v)
        if v > 1.0:  # models sometimes answer "85"
            v = v / 100.0
        return max(0.0, min(1.0, v))

    @field_validator("line_start", "line_end", mode="before")
    @classmethod
    def _coerce_line(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        return int(v)


class LLMReviewOutput(BaseModel):
    findings: list[LLMFinding] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    agent_type: AgentType
    severity: Severity
    category: str
    summary: str
    file_path: str
    line_start: int
    line_end: int
    suggestion: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence: str = ""

    # Filled in by the pipeline, never by a model.
    raw_confidence: float | None = None
    grounded: bool = False
    in_diff: bool = False
    source: str = "llm"  # llm | rules | guard
    corroborated_by: list[str] = Field(default_factory=list)
    status: FindingStatus = FindingStatus.PROPOSED
    adjustments: list[str] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        agent: AgentType,
        severity: Severity | str,
        category: str,
        summary: str,
        file_path: str,
        line_start: int,
        line_end: int | None = None,
        confidence: float,
        rationale: str,
        suggestion: str = "",
        evidence: str = "",
        source: str = "llm",
    ) -> Finding:
        sev = severity if isinstance(severity, Severity) else Severity.parse(severity)
        start = max(1, int(line_start))
        end = max(start, int(line_end) if line_end else start)
        cat = re.sub(r"[^a-z0-9]+", "-", category.strip().lower()).strip("-") or "general"
        conf = max(0.0, min(1.0, float(confidence)))
        return cls(
            id=finding_id(agent.value, file_path, start, cat, summary),
            agent_type=agent,
            severity=sev,
            category=cat,
            summary=summary.strip(),
            file_path=file_path.strip(),
            line_start=start,
            line_end=end,
            suggestion=suggestion.strip(),
            confidence=conf,
            raw_confidence=conf,
            rationale=rationale.strip(),
            evidence=evidence.strip(),
            source=source,
        )

    def adjust(self, factor: float, reason: str) -> None:
        """Scale confidence and leave an audit trail of why."""
        before = self.confidence
        self.confidence = max(0.0, min(0.99, self.confidence * factor))
        self.adjustments.append(f"{reason}: {before:.2f} -> {self.confidence:.2f}")

    @property
    def location(self) -> str:
        if self.line_end > self.line_start:
            return f"{self.file_path}:{self.line_start}-{self.line_end}"
        return f"{self.file_path}:{self.line_start}"


class AgentResult(BaseModel):
    agent: AgentType
    status: str = "ok"  # ok | degraded | timeout | error | skipped_budget
    findings: list[Finding] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    used_fallback: bool = False

    @property
    def completed(self) -> bool:
        """Did this specialist actually look at the diff (with a model or with rules)?"""
        return self.status in ("ok", "degraded")


class GateOutcome(BaseModel):
    decision: GateDecision
    overall_confidence: float
    coverage: float
    reasons: list[str] = Field(default_factory=list)
    to_post: list[str] = Field(default_factory=list)  # finding ids
    to_review: list[str] = Field(default_factory=list)
    suppressed: list[str] = Field(default_factory=list)
    priority: int = 0


class ReviewResult(BaseModel):
    review_id: str
    findings: list[Finding] = Field(default_factory=list)
    outcome: GateOutcome
    agents: list[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float = 0.0
    degraded: bool = False
