"""Regression gate: a change may not make the reviewer worse than the baseline.

Runs every golden case through the real pipeline (same nodes, same gate, same
publisher, with an in-memory store and a fake GitHub), scores the result and
compares it with ``baseline.json``. Exit code 1 blocks the merge.

With no API key the specialists run on their deterministic rule packs, which
is what CI uses: it pins the pipeline, the rules, the grounding and the gate.
With a key, the same command evaluates the prompts and models you configured
against a separate baseline entry, so the two modes never mask each other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.container import assemble, build_llm
from backend.database.memory_store import InMemoryStore
from backend.evaluation.golden_dataset import GOLDEN_CASES, GoldenCase
from backend.evaluation.judge import CaseScore, LLMJudge, Summary, match_case, summarise
from backend.integrations.fake_github import FakeGitHubClient
from backend.job_queue import jobs
from backend.models.schemas import HitlDecisionRequest, PRContext
from backend.observability.alerting import Alerter

BASELINE_PATH = Path(__file__).with_name("baseline.json")
RECALL_TOLERANCE = 0.02  # model output varies run to run; rules mode is exact


@dataclass
class EvalReport:
    mode: str
    summary: Summary
    scores: list[CaseScore]
    judge: dict[str, Any] | None = None
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "summary": self.summary.as_dict(),
            "cost_usd": round(self.cost_usd, 6),
            "judge": self.judge,
            "cases": [
                {
                    "id": s.case_id, "passed": s.passed, "matched": s.matched, "expected": s.expected,
                    "missed": s.missed, "forbidden_hits": s.forbidden_hits, "decision": s.decision,
                    "decision_ok": s.decision_ok, "leaks": s.leaks, "findings": s.findings, "surfaced": s.surfaced,
                }  # fmt: skip
                for s in self.scores
            ],
        }


def eval_mode(settings: Settings) -> str:
    provider = settings.resolved_llm_provider
    return "rules" if provider == "rules" else f"{provider}:{settings.model_for('security')}"


async def run_case(case: GoldenCase, settings: Settings, llm: Any | None) -> tuple[CaseScore, list[dict[str, Any]], float]:
    """Review one golden diff end to end and score it."""
    store = InMemoryStore()
    github = FakeGitHubClient()
    services = assemble(settings, store=store, github=github, llm=llm, alerter=Alerter(""))
    pr = PRContext(repo="local/golden", pr_number=1, head_sha=f"golden-{case.id}", title=case.title, body=case.description)
    files = [f.model_dump() for f in case.files()]
    github.add_pull(pr.repo, pr.pr_number, pr.head_sha, case.files(), title=case.title, body=case.description)
    result = await jobs.review_pull_request(services, {"pr": pr.model_dump(), "files": files})
    review_id = result["review_id"]
    review = await store.get_review(review_id) or {}
    findings = await store.get_findings(review_id)

    # If a human is needed, approve everything once so the leak check also
    # covers what would be published after approval.
    for item in await store.list_hitl("pending"):
        await services.approvals.decide(item["id"], HitlDecisionRequest(action="approve"), "golden-set")
    posted = json.dumps({"reviews": list(github.reviews.values()), "comments": list(github.comments.values())}, default=str)
    stored = json.dumps({"findings": await store.get_findings(review_id), "events": await store.get_trace(review_id)}, default=str)
    score = match_case(case, findings, str(review.get("decision") or ""), haystack=posted + stored)
    return score, findings, float(review.get("cost_usd") or 0.0)


async def run_eval(
    settings: Settings | None = None,
    *,
    cases: tuple[GoldenCase, ...] = GOLDEN_CASES,
    llm: Any | None = None,
    use_configured_llm: bool = True,
    judge: bool = False,
) -> EvalReport:
    settings = settings or Settings()
    # The golden set measures the reviewer, not the day's budget or a human's patience.
    settings = settings.model_copy(update={"autonomy_level": "exceptions_only", "daily_budget_usd": 0.0, "max_cost_per_review_usd": 0.0})
    if llm is None and use_configured_llm:
        llm = build_llm(settings)
    scores: list[CaseScore] = []
    cost = 0.0
    grades: list[dict[str, Any]] = []
    grader: LLMJudge | None = None
    if judge and llm is not None:
        from backend.prompts.registry import default_registry

        grader = LLMJudge(llm, settings.model_for("judge"), default_registry().get("judge").text, settings.llm_timeout_s)
    for case in cases:
        score, findings, case_cost = await run_case(case, settings, llm)
        scores.append(score)
        cost += case_cost
        if grader is not None:
            for finding in [f for f in findings if f["status"] != "suppressed"][:3]:
                try:
                    grades.append({"case": case.id, "finding": finding["finding_id"], **await grader.grade(case.diff, finding)})
                except Exception as exc:  # noqa: BLE001 - the judge is advisory
                    grades.append({"case": case.id, "finding": finding["finding_id"], "error": str(exc)[:200]})
    judged = None
    if grader is not None:
        ok = [g for g in grades if "error" not in g]
        judged = {
            "graded": len(ok),
            "errors": len(grades) - len(ok),
            "mean": {d: round(sum(g[d] for g in ok) / len(ok), 2) for d in LLMJudge.DIMENSIONS} if ok else {},
            "keep_rate": round(sum(1 for g in ok if g["verdict"] == "keep") / len(ok), 3) if ok else None,
            "grades": grades,
        }
    mode = "rules" if llm is None else eval_mode(settings)
    return EvalReport(mode=mode, summary=summarise(scores), scores=scores, judge=judged, cost_usd=cost)


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def check_against_baseline(report: EvalReport, baseline: dict[str, Any]) -> list[str]:
    """Return the list of regressions. Empty = the gate is green."""
    problems: list[str] = []
    s = report.summary
    if s.leaks:
        problems.append(f"{s.leaks} secret leak(s) in stored or posted output")
    entry = baseline.get(report.mode) or (baseline.get("rules") if report.mode != "rules" else None)
    if entry is None:
        problems.append(f"no baseline for mode '{report.mode}'; run with --update-baseline after reviewing the report")
        return problems
    tolerance = 0.0 if report.mode == "rules" else RECALL_TOLERANCE
    if s.recall + tolerance < entry["recall"]:
        problems.append(f"recall {s.recall:.2%} is below the baseline {entry['recall']:.2%}")
    if s.forbidden_hits > entry.get("forbidden_hits", 0):
        problems.append(f"{s.forbidden_hits} false alarm(s) on precision cases (baseline {entry.get('forbidden_hits', 0)})")
    if s.decision_accuracy + tolerance < entry.get("decision_accuracy", 1.0):
        problems.append(f"gate decision accuracy {s.decision_accuracy:.2%} is below the baseline {entry['decision_accuracy']:.2%}")
    for agent, floor in (entry.get("recall_by_agent") or {}).items():
        got = s.recall_by_agent.get(agent, 0.0)
        if got + tolerance < floor:
            problems.append(f"{agent} recall {got:.2%} is below the baseline {floor:.2%}")
    return problems


def update_baseline(report: EvalReport, path: Path = BASELINE_PATH) -> None:
    baseline = load_baseline(path)
    s = report.summary
    baseline[report.mode] = {
        "recall": s.recall,
        "forbidden_hits": s.forbidden_hits,
        "decision_accuracy": s.decision_accuracy,
        "recall_by_agent": s.recall_by_agent,
        "cases": s.cases,
    }
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
