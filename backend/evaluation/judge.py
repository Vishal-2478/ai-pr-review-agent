"""Scoring: did the review say what it should, and nothing it should not?

Two judges, used for different things:

- ``match_case`` is deterministic. It compares findings with the golden
  expectations by file, line and category. This is the regression gate: it
  gives the same answer every run, so a failing build means the system
  changed, not the grader's mood.
- ``LLMJudge`` asks a model to grade individual findings (correct, grounded,
  actionable, worth attention). It is advisory and only runs when a model is
  configured: useful for comparing prompts, too noisy to block a merge on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backend.evaluation.golden_dataset import Expected, GoldenCase
from backend.models.enums import Severity
from backend.tools.llm_client import LLMClient, extract_json

LINE_TOLERANCE = 3
_ACTIVE = {"proposed", "pending_human", "posted", "withheld"}  # everything except suppressed/rejected


@dataclass
class CaseScore:
    case_id: str
    expected: int = 0
    matched: int = 0
    missed: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    decision: str = ""
    decision_ok: bool = True
    leaks: list[str] = field(default_factory=list)
    findings: int = 0
    surfaced: int = 0
    by_agent: dict[str, list[int]] = field(default_factory=dict)  # agent -> [matched, expected]

    @property
    def passed(self) -> bool:
        return not (self.missed or self.forbidden_hits or self.leaks) and self.decision_ok


def _satisfies(finding: dict[str, Any], exp: Expected) -> bool:
    if finding["file_path"] != exp.file or finding["agent_type"] != exp.agent:
        return False
    if finding["category"] not in exp.categories:
        return False
    if Severity(finding["severity"]).rank < Severity(exp.min_severity).rank:
        return False
    if exp.line is None:
        return True
    return finding["line_start"] - LINE_TOLERANCE <= exp.line <= finding["line_end"] + LINE_TOLERANCE


def match_case(case: GoldenCase, findings: list[dict[str, Any]], decision: str, haystack: str = "") -> CaseScore:
    """Score one case. ``haystack`` is every string the system stored or posted (for leak checks)."""
    surfaced = [f for f in findings if f["status"] in _ACTIVE]
    score = CaseScore(case_id=case.id, expected=len(case.expect), decision=decision, findings=len(findings), surfaced=len(surfaced))
    for exp in case.expect:
        counts = score.by_agent.setdefault(exp.agent, [0, 0])
        counts[1] += 1
        # An expected finding must be surfaced, not merely generated and then suppressed.
        if any(_satisfies(f, exp) for f in surfaced):
            score.matched += 1
            counts[0] += 1
        else:
            where = f"{exp.file}:{exp.line}" if exp.line else exp.file
            score.missed.append(f"{exp.agent}/{'|'.join(exp.categories)} at {where}")
    for f in surfaced:
        if f["category"] in case.forbid:
            score.forbidden_hits.append(f"{f['agent_type']}/{f['category']} at {f['file_path']}:{f['line_start']}")
    if case.decisions:
        score.decision_ok = decision in case.decisions
    blob = haystack or json.dumps(findings, default=str)
    score.leaks = [secret[:12] + "…" for secret in case.must_not_leak if secret in blob]
    return score


@dataclass
class Summary:
    cases: int
    passed: int
    recall: float
    forbidden_hits: int
    decision_accuracy: float
    leaks: int
    recall_by_agent: dict[str, float]
    failed: list[str]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def summarise(scores: list[CaseScore]) -> Summary:
    expected = sum(s.expected for s in scores)
    matched = sum(s.matched for s in scores)
    by_agent: dict[str, list[int]] = {}
    for s in scores:
        for agent, (m, e) in s.by_agent.items():
            acc = by_agent.setdefault(agent, [0, 0])
            acc[0] += m
            acc[1] += e
    return Summary(
        cases=len(scores),
        passed=sum(1 for s in scores if s.passed),
        recall=round(matched / expected, 4) if expected else 1.0,
        forbidden_hits=sum(len(s.forbidden_hits) for s in scores),
        decision_accuracy=round(sum(1 for s in scores if s.decision_ok) / len(scores), 4) if scores else 1.0,
        leaks=sum(len(s.leaks) for s in scores),
        recall_by_agent={a: round(m / e, 4) if e else 1.0 for a, (m, e) in sorted(by_agent.items())},
        failed=[s.case_id for s in scores if not s.passed],
    )


class LLMJudge:
    """Advisory per-finding grading with the versioned ``judge`` prompt."""

    DIMENSIONS = ("correct", "grounded", "actionable", "worth_attention")

    def __init__(self, llm: LLMClient, model: str, system_prompt: str, timeout: float = 60.0) -> None:
        self._llm = llm
        self._model = model
        self._system = system_prompt
        self._timeout = timeout

    async def grade(self, diff: str, finding: dict[str, Any]) -> dict[str, Any]:
        shown = {
            k: finding.get(k)
            for k in ("severity", "category", "summary", "file_path", "line_start", "line_end", "evidence", "rationale", "suggestion", "confidence")
        }
        user = f"## Diff\n{diff}\n\n## Finding\n{json.dumps(shown, indent=2)}"
        response = await self._llm.complete_json(
            model=self._model, system=self._system, user=user, max_output_tokens=400, timeout=self._timeout, tag="judge"
        )
        data = extract_json(response.text)
        scores = {d: max(1, min(5, int(data.get(d, 1)))) for d in self.DIMENSIONS}
        return {**scores, "verdict": str(data.get("verdict", "")).lower(), "reason": str(data.get("reason", ""))[:300]}
