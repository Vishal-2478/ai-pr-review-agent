"""Workflow nodes: the steps of a review, independent of any framework.

Each node is ``async (state) -> partial state``. The LangGraph graph and the
plain-asyncio engine both drive these same functions.

Every side effect a node performs is idempotent (upserts, compare-and-set,
dedup-before-post), because a node can run twice: once before a crash and
once after the workflow resumes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.agents.aggregator import aggregate
from backend.agents.base_agent import AgentInput
from backend.agents.contracts import AgentResult, GateOutcome
from backend.core.exceptions import NodeTimeoutError
from backend.hitl.gate import apply_gate
from backend.models.enums import AgentType, EventType, FindingStatus, GateDecision, ReviewStatus, Severity
from backend.models.schemas import DiffFile, PRContext, RetrievedContext
from backend.reliability.timeout import with_timeout

if TYPE_CHECKING:
    from backend.container import Services

log = logging.getLogger(__name__)

MIN_COVERAGE_FOR_CLEAN = 0.5


class ReviewNodes:
    def __init__(self, services: Services) -> None:
        self.s = services

    # ------------------------------------------------------------ build_context
    async def build_context(self, state: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        pr = PRContext(**state["pr"])
        review_id = state["review_id"]
        async with s.emitter.span("build_context", agent="orchestrator", review_id=review_id) as span:
            await s.store.update_review(review_id, status=ReviewStatus.RUNNING.value)
            if state.get("files"):
                files = [DiffFile(**f) for f in state["files"]]  # supplied directly (CLI, evals, demo)
            else:
                files = await with_timeout(
                    s.github.list_pull_files(pr.repo, pr.pr_number, pr.installation_id, s.settings.max_files_per_review),
                    s.settings.github_timeout_s * 3, "fetch pull request files",
                )  # fmt: skip
            reviewable = [f for f in files if f.patch and f.status != "removed"]
            if not reviewable:
                span.set(files=len(files), skipped="no textual changes")
                return {"files": [], "context": RetrievedContext().model_dump(), "skip_reason": "no textual changes to review"}

            try:
                context = await with_timeout(
                    s.retriever.retrieve(
                        pr.repo, reviewable, review_id=review_id, top_k=s.settings.retrieval_top_k,
                        token_budget=s.settings.retrieval_token_budget,
                    ),
                    s.settings.retrieval_timeout_s, "context retrieval",
                )  # fmt: skip
            except NodeTimeoutError as exc:
                # Slower-but-correct beats stuck: review the diff without repository context.
                context = RetrievedContext(degraded=True, reason=str(exc))

            if context.indexed_files == 0 and s.queue is not None and not pr.repo.startswith("local/"):
                # First contact with this repository: build its memory in the background.
                try:
                    await s.queue.enqueue(
                        "index_repository", pr.repo, pr.base_ref or "HEAD", pr.installation_id, job_id=f"index:{pr.repo}"
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not enqueue indexing for %s: %s", pr.repo, exc)

            span.set(
                files=len(reviewable), additions=sum(f.additions for f in reviewable),
                deletions=sum(f.deletions for f in reviewable), context_chunks=len(context.chunks),
                context_degraded=context.degraded,
            )  # fmt: skip
            if context.degraded:
                span.outcome = "degraded"
            return {"files": [f.model_dump() for f in reviewable], "context": context.model_dump()}

    # ------------------------------------------------------------------ fan-out
    def plan_fan_out(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """One payload per specialist. Empty list = nothing to review."""
        if state.get("skip_reason") or not state.get("files"):
            return []
        shared = {k: state[k] for k in ("review_id", "pr", "files", "context")}
        return [{**shared, "agent": agent.value} for agent in AgentType.specialists()]

    async def specialist(self, payload: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        agent = s.specialists[payload["agent"]]
        inp = AgentInput(
            review_id=payload["review_id"],
            pr=PRContext(**payload["pr"]),
            files=[DiffFile(**f) for f in payload["files"]],
            context=RetrievedContext(**payload["context"]),
        )
        try:
            result = await with_timeout(agent.run(inp), s.settings.agent_timeout_s, f"{agent.name} specialist")
        except NodeTimeoutError as exc:
            # The join must never wait forever on one hung agent.
            await s.emitter.emit(
                EventType.ERROR, agent=agent.name, name="node_timeout", review_id=inp.review_id,
                outcome="fallback_to_rules", error=str(exc),
            )  # fmt: skip
            result = agent.fallback_result(inp, status="timeout", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a broken specialist yields an empty result, not a dead review
            log.exception("%s specialist crashed", agent.name)
            await s.emitter.emit(
                EventType.ERROR, agent=agent.name, name="node_error", review_id=inp.review_id,
                outcome="empty_result", error=f"{type(exc).__name__}: {exc}"[:500],
            )  # fmt: skip
            result = AgentResult(agent=agent.agent_type, status="error", error=f"{type(exc).__name__}: {exc}"[:300])
        return {"agent_results": [result.model_dump(mode="json")]}

    # ---------------------------------------------------------------- aggregate
    async def aggregate(self, state: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        pr = PRContext(**state["pr"])
        review_id = state["review_id"]
        async with s.emitter.span("aggregate", agent="aggregator", review_id=review_id) as span:
            # Keep one result per agent even if a resumed run delivered a duplicate.
            by_agent: dict[str, AgentResult] = {}
            for raw in state.get("agent_results", []):
                result = AgentResult(**raw)
                by_agent[result.agent.value] = result
            results = list(by_agent.values())
            try:
                multipliers = await s.calibrator.multipliers(pr.repo)
            except Exception as exc:  # noqa: BLE001 - calibration is an optimisation, not a dependency
                log.warning("feedback calibration unavailable: %s", exc)
                multipliers = {}

            context = RetrievedContext(**state.get("context", {}))
            agg = aggregate(results, multipliers=multipliers, context_degraded=context.degraded)
            injection = any(f.category == "prompt-injection" for f in agg.findings)
            outcome = apply_gate(agg.findings, agg.coverage, s.settings, injection_detected=injection)

            await s.store.save_findings(review_id, pr.repo, pr.pr_number, agg.findings)
            counts: dict[str, int] = {}
            for f in agg.findings:
                if f.status is not FindingStatus.SUPPRESSED:
                    counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
            await s.store.update_review(
                review_id, decision=outcome.decision.value, overall_confidence=outcome.overall_confidence,
                coverage=outcome.coverage, reasons=outcome.reasons, cost_usd=agg.cost_usd,
                summary={
                    "findings": counts, "suppressed": len(outcome.suppressed), "merged": agg.merged,
                    "agents": agg.agents, "context_degraded": context.degraded,
                    "context_chunks": len(context.chunks),
                },
            )  # fmt: skip
            await s.emitter.emit(
                EventType.DECISION, agent="aggregator", name="gate", review_id=review_id,
                outcome=outcome.decision.value, confidence=outcome.overall_confidence,
                coverage=outcome.coverage, reasons=outcome.reasons, to_post=outcome.to_post,
                to_review=outcome.to_review, suppressed=outcome.suppressed,
                threshold=s.settings.hitl_confidence_threshold, autonomy=s.settings.autonomy_level,
            )  # fmt: skip
            span.confidence = outcome.overall_confidence
            span.set(decision=outcome.decision.value, findings=len(agg.findings), merged=agg.merged)
            criticals = [
                f"{f.location}: {f.summary}" for f in agg.findings
                if f.severity is Severity.CRITICAL and f.status is not FindingStatus.SUPPRESSED
            ]  # fmt: skip
            return {"outcome": {**outcome.model_dump(mode="json"), "critical_summaries": criticals}}

    def route_after_gate(self, state: dict[str, Any]) -> str:
        decision = GateDecision(state["outcome"]["decision"])
        if decision is GateDecision.AUTO_POST:
            return "publish"
        if decision in (GateDecision.HUMAN_REVIEW, GateDecision.ESCALATE):
            return "queue_human"
        return "finalize"

    # ----------------------------------------------------------------- endings
    async def publish(self, state: dict[str, Any]) -> dict[str, Any]:
        result = await self.s.publisher.publish(state["review_id"], state["outcome"]["to_post"])
        return {"final_status": ReviewStatus.POSTED.value if result["posted"] else ReviewStatus.RESOLVED.value}

    async def queue_human(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = dict(state["outcome"])
        criticals = raw.pop("critical_summaries", [])
        outcome = GateOutcome(**raw)
        status = ReviewStatus.ESCALATED if outcome.decision is GateDecision.ESCALATE else ReviewStatus.PENDING_HUMAN
        await self.s.approvals.submit(state["review_id"], PRContext(**state["pr"]), outcome, criticals)
        await self.s.store.update_review(state["review_id"], status=status.value)
        return {"final_status": status.value}

    async def finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        review_id = state["review_id"]
        outcome = state.get("outcome") or {}
        if state.get("skip_reason"):
            await s.store.update_review(
                review_id, completed=True, status=ReviewStatus.SKIPPED.value, reasons=[state["skip_reason"]]
            )
            return {"final_status": ReviewStatus.SKIPPED.value}
        if outcome.get("decision") == GateDecision.SHADOW.value:
            await s.store.update_review(review_id, completed=True, status=ReviewStatus.SHADOW.value)
            return {"final_status": ReviewStatus.SHADOW.value}
        coverage = float(outcome.get("coverage", 0.0))
        if coverage < MIN_COVERAGE_FOR_CLEAN:
            # "No findings" from specialists that mostly failed is not a clean bill of health.
            await s.store.update_review(
                review_id, completed=True, status=ReviewStatus.FAILED.value,
                error=f"insufficient coverage ({coverage:.0%}): too few specialists completed to call this clean",
            )  # fmt: skip
            return {"final_status": ReviewStatus.FAILED.value}
        if s.settings.post_clean_reviews:
            await s.publisher.publish(review_id, [])
        await s.store.update_review(review_id, completed=True, status=ReviewStatus.CLEAN.value)
        return {"final_status": ReviewStatus.CLEAN.value}
