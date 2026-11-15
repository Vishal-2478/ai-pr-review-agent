"""Closed vocabularies shared by every module."""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @property
    def weight(self) -> float:
        """How much a finding of this severity counts towards overall confidence."""
        return _SEVERITY_WEIGHT[self]

    @classmethod
    def parse(cls, value: str) -> Severity:
        v = (value or "").strip().lower()
        aliases = {"blocker": "critical", "major": "high", "moderate": "medium", "minor": "low", "nit": "info"}
        return cls(aliases.get(v, v))


_SEVERITY_RANK = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}
_SEVERITY_WEIGHT = {
    Severity.CRITICAL: 5.0,
    Severity.HIGH: 3.0,
    Severity.MEDIUM: 2.0,
    Severity.LOW: 1.0,
    Severity.INFO: 0.5,
}


class AgentType(str, Enum):
    SECURITY = "security"
    QUALITY = "quality"
    TESTS = "tests"
    DOCS = "docs"
    AGGREGATOR = "aggregator"

    @classmethod
    def specialists(cls) -> tuple[AgentType, ...]:
        return (cls.SECURITY, cls.QUALITY, cls.TESTS, cls.DOCS)


class GateDecision(str, Enum):
    AUTO_POST = "auto_post"
    HUMAN_REVIEW = "human_review"
    ESCALATE = "escalate"
    NO_FINDINGS = "no_findings"
    SHADOW = "shadow"


class ReviewStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    POSTED = "posted"
    PENDING_HUMAN = "pending_human"
    ESCALATED = "escalated"
    REJECTED = "rejected"
    RESOLVED = "resolved"  # a human handled it; nothing was posted publicly
    CLEAN = "clean"
    SHADOW = "shadow"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self not in (ReviewStatus.QUEUED, ReviewStatus.RUNNING)


class FindingStatus(str, Enum):
    PROPOSED = "proposed"  # passed the gate, not yet published
    SUPPRESSED = "suppressed"  # below the noise floor or not worth attention
    PENDING_HUMAN = "pending_human"
    POSTED = "posted"
    WITHHELD = "withheld"  # accepted but deliberately kept out of the public thread
    REJECTED = "rejected"  # a human said no
    DISPUTED = "disputed"  # a developer pushed back after posting
    UPHELD = "upheld"  # dispute resolved in the agent's favour
    RETRACTED = "retracted"  # dispute resolved in the developer's favour


class EventType(str, Enum):
    SPAN_START = "span.start"
    SPAN_END = "span.end"
    LLM_CALL = "llm.call"
    TOOL_CALL = "tool.call"
    DECISION = "decision"
    ESCALATION = "escalation"
    FEEDBACK = "feedback"
    ERROR = "error"
    AUDIT = "audit"


class HitlKind(str, Enum):
    APPROVAL = "approval"
    ESCALATION = "escalation"
    DISPUTE = "dispute"


class HitlStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Verdict(str, Enum):
    """What a human said about one finding."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISPUTED = "disputed"


class Role(str, Enum):
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ADMIN = "admin"
