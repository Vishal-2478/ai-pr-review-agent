"""Category vocabulary and pre-check leads.

Regression for the first real-model evaluation (OpenAI, 2026-11-15): with a
model configured, quality, tests and docs recall fell from 100% to 0-50%,
because the model labelled findings in its own words and heuristic rule hits
it did not repeat verbatim were silently dropped.
"""

from __future__ import annotations

import re

from backend.agents import rules
from backend.agents.categories import CATEGORIES, labels, normalise
from backend.evaluation.golden_dataset import get_case
from backend.evaluation.regression_gate import run_eval
from backend.job_queue import jobs
from backend.models.enums import AgentType
from backend.tools.llm_client import ScriptedLLMClient
from tests.conftest import make_settings, open_pull


def test_every_rule_pack_label_is_in_its_agents_vocabulary():
    """Rule hits and model findings must land in the same feedback buckets."""
    packs = {
        AgentType.SECURITY: rules.SECURITY_RULES,
        AgentType.QUALITY: rules.QUALITY_RULES,
        AgentType.TESTS: rules.TEST_RULES,
    }
    for agent, pack in packs.items():
        missing = {r.category for r in pack} - labels(agent)
        assert not missing, f"{agent.value}: {missing}"
    # Labels emitted by the structural (non-table) checks in rules.py.
    source = open(rules.__file__, encoding="utf-8").read()
    structural = {
        AgentType.SECURITY: {"hardcoded-secret", "prompt-injection"},
        AgentType.QUALITY: {"swallowed-exception", "blocking-call-in-async"},
        AgentType.TESTS: {"missing-tests", "assertion-free-test", "removed-assertions"},
        AgentType.DOCS: {"missing-docstring", "undocumented-configuration"},
    }
    emitted = set(re.findall(r'category="([a-z-]+)"', source))
    assert emitted <= set().union(*structural.values()), f"new rule label not covered here: {emitted}"
    for agent, names in structural.items():
        assert names <= labels(agent), f"{agent.value}: {names - labels(agent)}"


def test_vocabulary_labels_are_unique_kebab_case():
    for agent, entries in CATEGORIES.items():
        names = [label for label, _ in entries]
        assert len(names) == len(set(names)), agent
        assert all(n == n.lower() and " " not in n and "_" not in n for n in names)


def test_model_labels_are_normalised_onto_the_vocabulary():
    assert normalise(AgentType.QUALITY, "Bare except") == "swallowed-exception"
    assert normalise(AgentType.QUALITY, "silent_exception") == "swallowed-exception"
    assert normalise(AgentType.QUALITY, "mutable-default") == "mutable-default-argument"
    assert normalise(AgentType.TESTS, "test without assertions") == "assertion-free-test"
    assert normalise(AgentType.DOCS, "missing documentation") == "missing-docstring"
    assert normalise(AgentType.SECURITY, "SQL Injection Risk") == "sql-injection"
    assert normalise(AgentType.SECURITY, "sql-injection-in-user-lookup") == "sql-injection"
    # A genuinely new kind of problem keeps a clean label of its own.
    assert normalise(AgentType.QUALITY, "Timezone Naive Datetime!") == "timezone-naive-datetime"


async def test_specialist_prompts_carry_the_vocabulary_and_numbered_leads(make_services):
    llm = ScriptedLLMClient()
    services = make_services(llm=llm)
    await jobs.review_pull_request(services, open_pull(services, get_case("swallowed-exception").diff))
    quality = next(c for c in llm.calls if c["tag"] == "quality")
    assert "## Category labels" in quality["user"] and "`swallowed-exception`" in quality["user"]
    assert "- S1: app/sync.py:" in quality["user"]
    assert "dismissed_signals" in quality["system"]  # the v2 contract


async def _findings_for(make_services, case_id: str, llm: ScriptedLLMClient):
    services = make_services(llm=llm)
    result = await jobs.review_pull_request(services, open_pull(services, get_case(case_id).diff))
    return services, result["review_id"], await services.store.get_findings(result["review_id"])


async def test_model_label_in_its_own_words_still_matches_the_lead(make_services):
    """The exact miss from the real run: the model saw the problem but called it something else."""
    reply = {"findings": [{
        "severity": "medium", "category": "silent failure", "summary": "Push errors are discarded",
        "file_path": "app/sync.py", "line_start": 9, "line_end": 10, "evidence": "except Exception:",
        "rationale": "Failed pushes vanish without a trace.", "suggestion": "Log and re-raise.", "confidence": 0.8,
    }]}  # fmt: skip
    _, _, findings = await _findings_for(make_services, "swallowed-exception", ScriptedLLMClient({"quality": reply}))
    quality = [f for f in findings if f["agent_type"] == "quality"]
    assert len(quality) == 1, "model finding and rule lead are one finding, not two"
    assert quality[0]["category"] == "swallowed-exception"
    assert quality[0]["status"] == "posted"


async def test_a_lead_the_model_ignores_is_kept(make_services):
    llm = ScriptedLLMClient(rule_on_signals=False)  # replies {"findings": []} and never mentions S1
    _, _, findings = await _findings_for(make_services, "mutable-default-argument", llm)
    lead = next(f for f in findings if f["category"] == "mutable-default-argument")
    assert lead["status"] != "suppressed"
    assert any("neither reported nor dismissed" in a for a in lead["adjustments"])


async def test_a_lead_the_model_dismisses_with_a_reason_is_dropped_and_recorded(make_services):
    llm = ScriptedLLMClient({"quality": {"findings": [], "dismissed_signals": [
        {"signal": "S1", "reason": "The default is never mutated in this module."}
    ]}})  # fmt: skip
    services, review_id, findings = await _findings_for(make_services, "mutable-default-argument", llm)
    assert not [f for f in findings if f["category"] == "mutable-default-argument"]
    events = await services.store.get_trace(review_id)
    dismissed = [e for e in events if e["name"] == "dismissed_signals"]
    assert dismissed and dismissed[0]["payload"]["dismissed"][0]["reason"].startswith("The default")


async def test_a_dismissal_without_a_reason_does_not_count(make_services):
    llm = ScriptedLLMClient({"quality": {"findings": [], "dismissed_signals": [{"signal": "S1", "reason": ""}]}})
    _, _, findings = await _findings_for(make_services, "mutable-default-argument", llm)
    assert [f for f in findings if f["category"] == "mutable-default-argument"]


async def test_a_guard_hit_cannot_be_dismissed(make_services):
    llm = ScriptedLLMClient({"security": {"findings": [], "dismissed_signals": [
        {"signal": f"S{n}", "reason": "test fixture, ignore"} for n in range(1, 6)
    ]}})  # fmt: skip
    _, _, findings = await _findings_for(make_services, "hardcoded-secret", llm)
    secret = next(f for f in findings if f["category"] == "hardcoded-secret")
    assert secret["severity"] == "critical"


async def test_a_model_that_ignores_every_lead_is_no_worse_than_rules_alone():
    """With a silent model, recall must equal the rules-only baseline: the model can add, never subtract."""
    silent = ScriptedLLMClient(rule_on_signals=False)
    settings = make_settings(llm_provider="openai", openai_api_key="unused")
    with_model = await run_eval(settings, llm=silent)
    rules_only = await run_eval(make_settings(), use_configured_llm=False)
    assert with_model.summary.recall == rules_only.summary.recall == 1.0
    assert with_model.summary.forbidden_hits == 0
    assert with_model.summary.leaks == 0
