"""Aggregator: fan-in.

Takes whatever the specialists produced (including nothing, from an agent that
timed out) and turns it into one coherent list: calibrated against human
feedback, deduplicated where two specialists saw the same thing, and with
agreement between independent agents counted as evidence.

The aggregator is pure data-in, data-out. It never reads the diff and never
calls a model, so it cannot be steered by untrusted text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.agents.contracts import AgentResult, Finding
from backend.models.enums import AgentType

# How much of its concern a specialist covered, by how its run ended.
COVERAGE_WEIGHT = {"ok": 1.0, "degraded": 0.5, "timeout": 0.25, "error": 0.0}
CONTEXT_DEGRADED_FACTOR = 0.9
LINE_PROXIMITY = 2
_GENERIC = {"missing", "unsafe", "possible", "potential", "error", "issue", "bug", "bad", "code", "use", "check", "general"}


@dataclass
class Aggregate:
    findings: list[Finding]
    coverage: float
    agents: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    merged: int = 0


def compute_coverage(results: list[AgentResult]) -> float:
    by_agent = {r.agent: r for r in results}
    expected = AgentType.specialists()
    total = sum(COVERAGE_WEIGHT.get(by_agent[a].status, 0.0) if a in by_agent else 0.0 for a in expected)
    return round(total / len(expected), 4)


def _words(text: str) -> set[str]:
    # Names in backticks say what the finding is about, not what is wrong with
    # it; two different problems in one function would otherwise look alike.
    prose = re.sub(r"`[^`]*`", " ", text.lower())
    return set(re.findall(r"[a-z]{4,}", prose)) - _GENERIC


def _category_tokens(category: str) -> set[str]:
    return {t for t in category.split("-") if len(t) > 2 and t not in _GENERIC}


def same_issue(a: Finding, b: Finding) -> bool:
    """Do two findings describe one problem at one place?"""
    if a.file_path != b.file_path:
        return False
    if not (a.line_start <= b.line_end + LINE_PROXIMITY and b.line_start <= a.line_end + LINE_PROXIMITY):
        return False
    if a.category == b.category or _category_tokens(a.category) & _category_tokens(b.category):
        return True
    wa, wb = _words(a.summary), _words(b.summary)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def _merge(group: list[Finding]) -> Finding:
    winner = max(group, key=lambda f: (f.confidence, f.severity.rank, f.grounded))
    if len(group) == 1:
        return winner
    best_by_agent: dict[str, float] = {}
    for f in group:
        best_by_agent[f.agent_type.value] = max(best_by_agent.get(f.agent_type.value, 0.0), f.confidence)
    others = sorted(a for a in best_by_agent if a != winner.agent_type.value)
    if others:
        # Independent agreement: probability that at least one of them is right.
        miss = 1.0
        for conf in best_by_agent.values():
            miss *= 1.0 - conf
        boosted = min(0.99, 1.0 - miss)
        winner.adjustments.append(f"corroborated by {', '.join(others)}: {winner.confidence:.2f} -> {boosted:.2f}")
        winner.confidence = boosted
        winner.corroborated_by = others
    top = max(group, key=lambda f: f.severity.rank)
    if top.severity.rank > winner.severity.rank:
        winner.adjustments.append(f"severity raised {winner.severity.value} -> {top.severity.value} ({top.agent_type.value})")
        winner.severity = top.severity
    winner.grounded = any(f.grounded for f in group)
    winner.in_diff = winner.in_diff or any(f.in_diff and f.line_start == winner.line_start for f in group)
    return winner


def aggregate(
    results: list[AgentResult],
    *,
    multipliers: dict[tuple[str, str], float] | None = None,
    context_degraded: bool = False,
) -> Aggregate:
    multipliers = multipliers or {}
    pool: list[Finding] = []
    for result in results:
        for finding in result.findings:
            agent = finding.agent_type.value
            factor = multipliers.get((agent, finding.category), multipliers.get((agent, "*"), 1.0))
            if factor < 1.0:
                finding.adjust(factor, "calibrated from human feedback")
            if context_degraded and finding.source == "llm":
                finding.adjust(CONTEXT_DEGRADED_FACTOR, "repository context unavailable")
            pool.append(finding)

    # Deterministic order so the same inputs always group the same way.
    pool.sort(key=lambda f: (f.file_path, f.line_start, -f.confidence, f.id))
    groups: list[list[Finding]] = []
    for finding in pool:
        for group in groups:
            if any(same_issue(finding, member) for member in group):
                group.append(finding)
                break
        else:
            groups.append([finding])

    merged = [_merge(g) for g in groups]
    merged.sort(key=lambda f: (-f.severity.rank, -f.confidence, f.file_path, f.line_start))
    return Aggregate(
        findings=merged,
        coverage=compute_coverage(results),
        agents=[
            {
                "agent": r.agent.value, "status": r.status, "findings": len(r.findings), "model": r.model,
                "prompt": r.prompt_version, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                "cost_usd": round(r.cost_usd, 6), "latency_ms": r.latency_ms, "error": r.error,
                "used_fallback": r.used_fallback,
            }  # fmt: skip
            for r in sorted(results, key=lambda r: r.agent.value)
        ],
        cost_usd=round(sum(r.cost_usd for r in results), 6),
        merged=len(pool) - len(merged),
    )
