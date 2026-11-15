"""Shared specialist machinery.

All four specialists run the same loop and differ only in their prompt, the
files they care about and their post-processing:

    pre-check signals -> budget check -> route model -> call (retry + breaker)
        -> validate against the Finding contract -> ground against the diff

Every step emits an event, and every way a model can fail (timeout, outage,
garbage output, exhausted budget) degrades to the deterministic rule pack
rather than failing the review.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from backend.agents.contracts import AgentResult, Finding, LLMReviewOutput
from backend.agents.grounding import ground_findings
from backend.agents.rules import run_rules
from backend.config import Settings
from backend.core.exceptions import OutputValidationError, ReviewAgentError
from backend.economics.budget_guard import BudgetGuard, BudgetStatus
from backend.economics.pricing import cost_usd
from backend.integrations.diff import is_doc_path, is_source_path, is_test_path, render_diff
from backend.models.enums import AgentType, EventType, Severity
from backend.models.schemas import DiffFile, PRContext, RetrievedContext
from backend.observability.events import EventEmitter
from backend.prompts.registry import PromptRegistry
from backend.reliability.circuit_breaker import BreakerRegistry
from backend.reliability.retry import is_retryable, retry_async
from backend.security.injection_guard import new_boundary, notice, wrap_untrusted
from backend.security.secret_masking import mask
from backend.tools.llm_client import LLMClient, extract_json
from backend.tools.model_router import ModelRouter
from backend.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

MAX_FINDINGS_PER_AGENT = 12


@dataclass
class AgentDeps:
    settings: Settings
    llm: LLMClient | None  # None = rules-only mode
    prompts: PromptRegistry
    emitter: EventEmitter
    budget: BudgetGuard
    router: ModelRouter
    breakers: BreakerRegistry
    tools: ToolRegistry


@dataclass
class AgentInput:
    review_id: str
    pr: PRContext
    files: list[DiffFile]
    context: RetrievedContext


class SpecialistAgent:
    agent_type: AgentType
    max_severity: Severity = Severity.CRITICAL

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    # ------------------------------------------------------------ overridable
    def relevant_files(self, files: list[DiffFile]) -> list[DiffFile]:
        return files

    def postprocess(self, findings: list[Finding]) -> list[Finding]:
        for f in findings:
            if f.severity.rank > self.max_severity.rank:
                f.adjustments.append(f"severity capped {f.severity.value} -> {self.max_severity.value}")
                f.severity = self.max_severity
        return findings

    # -------------------------------------------------------------------- run
    @property
    def name(self) -> str:
        return self.agent_type.value

    async def run(self, inp: AgentInput) -> AgentResult:
        started = time.perf_counter()
        async with self.deps.emitter.span("specialist", agent=self.name, review_id=inp.review_id) as span:
            pr_text = f"{inp.pr.title}\n{inp.pr.body}"
            signals: list[Finding] = await self.deps.tools.call(
                self.name, "static_signals", agent=self.agent_type, files=inp.files, pr_text=pr_text
            )
            relevant = self.relevant_files(inp.files)

            if self.deps.llm is None:
                result = self._from_rules(signals, status="ok")
            elif not relevant:
                result = self._from_rules(signals, status="ok")
                result.error = "no files relevant to this concern; model call skipped"
            else:
                budget = await self.deps.budget.check(inp.review_id)
                if not budget.allowed:
                    await self.deps.emitter.emit(
                        EventType.DECISION, agent=self.name, name="budget_blocked", outcome="blocked",
                        reason=budget.reason, spent_today=budget.spent_today, daily_cap=budget.daily_cap,
                    )  # fmt: skip
                    result = self._from_rules(signals, status="degraded", error=budget.reason)
                    result.used_fallback = True
                else:
                    try:
                        result = await self._run_models(inp, relevant, signals, budget)
                    except Exception as exc:  # noqa: BLE001 - every model failed; degrade, do not die
                        await self.deps.emitter.emit(
                            EventType.ERROR, agent=self.name, name="llm_unavailable", outcome="fallback_to_rules",
                            error=f"{type(exc).__name__}: {exc}"[:500],
                        )  # fmt: skip
                        result = self._from_rules(signals, status="degraded", error=f"{type(exc).__name__}: {exc}"[:300])
                        result.used_fallback = True

            result.findings = ground_findings(self.postprocess(result.findings), inp.files, inp.context)
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            span.outcome = result.status
            span.set(findings=len(result.findings), model=result.model, cost_usd=result.cost_usd, fallback=result.used_fallback)
            return result

    def fallback_result(self, inp: AgentInput, status: str, error: str) -> AgentResult:
        """What the workflow uses when this specialist blew its time budget."""
        signals = run_rules(self.agent_type, inp.files, f"{inp.pr.title}\n{inp.pr.body}")
        result = self._from_rules(signals, status=status, error=error)
        result.used_fallback = True
        result.findings = ground_findings(self.postprocess(result.findings), inp.files, inp.context)
        return result

    def _from_rules(self, signals: list[Finding], status: str, error: str | None = None) -> AgentResult:
        return AgentResult(agent=self.agent_type, status=status, findings=list(signals), model="rules", error=error)

    # ------------------------------------------------------------- model path
    async def _run_models(
        self, inp: AgentInput, relevant: list[DiffFile], signals: list[Finding], budget: BudgetStatus
    ) -> AgentResult:
        settings = self.deps.settings
        diff_text, omitted = render_diff(relevant, settings.max_diff_chars)
        boundary = new_boundary()
        system, prompt_ref = self.deps.prompts.system_for(self.name)
        system = f"{system}\n\n## Untrusted input\n{notice(boundary)}"
        user = self._user_prompt(inp, signals, mask(diff_text), omitted, boundary)
        choice = self.deps.router.select(self.name, diff_chars=len(diff_text), budget=budget)

        result = AgentResult(agent=self.agent_type, prompt_version=prompt_ref)
        last_error: Exception | None = None
        for model in choice.models:
            try:
                parsed = await self._call_and_parse(model, system, user, result, inp, prompt_ref, choice.reason)
            except ReviewAgentError as exc:
                last_error = exc
                log.warning("%s: model %s failed: %s", self.name, model, exc)
                continue
            result.model = model
            result.findings = self._to_findings(parsed, signals)
            if omitted:
                result.status = "degraded"
                result.error = f"{len(omitted)} file(s) left out of the prompt for size: {', '.join(omitted[:5])}"
            return result
        raise last_error or ReviewAgentError("no model available")

    async def _call_and_parse(
        self, model: str, system: str, user: str, result: AgentResult, inp: AgentInput, prompt_ref: str, reason: str
    ) -> LLMReviewOutput:
        try:
            return self._parse(await self._call(model, system, user, result, inp, prompt_ref, reason, "review"))
        except OutputValidationError as exc:
            # One repair round: tell the model exactly what was wrong with its reply.
            repair = (
                f"{user}\n\n## Your previous reply was rejected\n{exc}\n"
                "Reply again with only the JSON object described in the instructions."
            )
            return self._parse(await self._call(model, system, repair, result, inp, prompt_ref, reason, "repair"))

    async def _call(
        self, model: str, system: str, user: str, result: AgentResult, inp: AgentInput,
        prompt_ref: str, reason: str, kind: str,
    ) -> str:  # fmt: skip
        settings = self.deps.settings
        breaker = self.deps.breakers.get(f"llm:{model}")
        llm = self.deps.llm
        assert llm is not None

        async def attempt() -> Any:
            return await breaker.call(
                lambda: llm.complete_json(
                    model=model, system=system, user=user, max_output_tokens=settings.llm_max_output_tokens,
                    timeout=settings.llm_timeout_s, tag=self.name,
                ),
                counts_as_failure=is_retryable,
            )  # fmt: skip

        started = time.perf_counter()
        try:
            response = await retry_async(
                attempt, attempts=settings.retry_attempts, base_delay=settings.retry_base_delay_s
            )
        except Exception as exc:
            await self.deps.emitter.emit(
                EventType.LLM_CALL, agent=self.name, name=kind, model=model, outcome="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:500], prompt=prompt_ref, routing=reason,
            )  # fmt: skip
            raise
        cost = cost_usd(model, response.tokens_in, response.tokens_out)
        result.tokens_in += response.tokens_in
        result.tokens_out += response.tokens_out
        result.cost_usd += cost
        await self.deps.emitter.emit(
            EventType.LLM_CALL, agent=self.name, name=kind, model=model, outcome="ok",
            tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=cost,
            latency_ms=response.latency_ms, prompt=prompt_ref, routing=reason,
            context_chunks=inp.context.chunk_ids(), context_degraded=inp.context.degraded,
        )  # fmt: skip
        return response.text

    @staticmethod
    def _parse(text: str) -> LLMReviewOutput:
        try:
            return LLMReviewOutput.model_validate(extract_json(text))
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
            raise OutputValidationError(f"reply does not match the findings schema: {problems}") from exc

    def _to_findings(self, parsed: LLMReviewOutput, signals: list[Finding]) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()
        for raw in parsed.findings:
            if not raw.summary.strip() or not raw.file_path.strip():
                continue
            try:
                severity = Severity.parse(raw.severity)
            except ValueError:
                severity = Severity.MEDIUM
            finding = Finding.build(
                agent=self.agent_type, severity=severity, category=raw.category, summary=raw.summary,
                file_path=raw.file_path, line_start=raw.line_start, line_end=raw.line_end,
                confidence=raw.confidence, rationale=raw.rationale, suggestion=raw.suggestion,
                evidence=raw.evidence, source="llm",
            )  # fmt: skip
            if finding.id not in seen:
                seen.add(finding.id)
                findings.append(finding)
        findings.sort(key=lambda f: (-f.severity.rank, -f.confidence))
        findings = findings[:MAX_FINDINGS_PER_AGENT]
        # High-precision guard hits (real key formats, injection attempts) are
        # reported even if the model chose to stay silent about them.
        for signal in signals:
            if signal.source == "guard" and not any(
                f.file_path == signal.file_path and abs(f.line_start - signal.line_start) <= 1
                and f.category == signal.category for f in findings
            ):  # fmt: skip
                findings.append(signal)
        return findings

    def _user_prompt(
        self, inp: AgentInput, signals: list[Finding], diff_text: str, omitted: list[str], boundary: str
    ) -> str:
        pr, ctx = inp.pr, inp.context
        parts = [
            f"## Pull request\nRepository: {pr.repo}\nBase branch: {pr.base_ref or 'unknown'}",
            wrap_untrusted("pull request title and description", mask(f"{pr.title}\n\n{pr.body}"[:4000]), boundary),
        ]
        if ctx.conventions:
            parts.append("## Team conventions\n" + "\n".join(f"- {c}" for c in ctx.conventions[:30]))
        if ctx.history:
            parts.append(
                "## How past findings on these files landed\n"
                + "\n".join(f"- [{h.status}] {h.agent_type}/{h.category} in {h.file_path}: {h.summary}" for h in ctx.history[:12])
            )
        if ctx.chunks:
            code = "\n\n".join(
                f"### {c.path}{' :: ' + c.symbol if c.symbol else ''} (lines {c.start_line}-{c.end_line})\n{c.content}"
                for c in ctx.chunks
            )
            parts.append("## Existing code retrieved for this change\n" + wrap_untrusted("repository code", mask(code), boundary))
        elif ctx.degraded:
            parts.append(
                "## Existing code\nNo repository context could be retrieved for this review "
                f"({ctx.reason or 'index unavailable'}). You are seeing the diff only: lower your confidence "
                "for anything that depends on code outside it."
            )
        if signals:
            parts.append(
                "## Pre-check signals for your concern (leads to verify, not conclusions)\n"
                + "\n".join(f"- {s.location} [{s.category}] {s.summary}" for s in signals[:20])
            )
        parts.append("## Diff\n" + wrap_untrusted("diff", diff_text, boundary))
        if omitted:
            parts.append("Files in this pull request that were too large to include: " + ", ".join(omitted[:20]))
        return "\n\n".join(parts)


def source_and_tests(files: list[DiffFile]) -> list[DiffFile]:
    return [f for f in files if is_source_path(f.path) or is_test_path(f.path)]


def source_and_docs(files: list[DiffFile]) -> list[DiffFile]:
    return [f for f in files if is_source_path(f.path) or is_doc_path(f.path)]
