"""M4-M7: the review pipeline end to end, with a scripted model."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.core.exceptions import RetryableError
from backend.job_queue import jobs
from backend.tools.llm_client import ScriptedLLMClient
from tests.conftest import CLEAN_DIFF, SQLI_DIFF, llm_finding, make_settings, open_pull


async def _review(services, job) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    result = await jobs.review_pull_request(services, job)
    review = await services.store.get_review(result["review_id"])
    return result, review, await services.store.get_findings(result["review_id"])


async def test_confident_grounded_finding_is_posted_inline(make_services):
    llm = ScriptedLLMClient({"security": {"findings": [llm_finding()]}})
    services = make_services(llm=llm)
    result, review, findings = await _review(services, open_pull(services))

    assert review["decision"] == "auto_post" and review["status"] == "posted"
    posted = services.github.reviews[("acme/shop", 1)]
    assert len(posted) == 1
    sqli = [c for c in posted[0]["comments"] if "sql-injection" in c["body"]]
    assert sqli and sqli[0]["path"] == "app/users.py" and sqli[0]["line"] == 9
    assert {f["agent_type"] for f in findings} >= {"security"}
    # Every specialist ran, once, in the same review.
    assert sorted({c["tag"] for c in llm.calls}) == ["docs", "quality", "security", "tests"]


async def test_specialists_run_in_parallel(make_services):
    """Fan-out: four model calls overlap instead of queueing behind each other."""
    running = 0
    peak = 0

    async def slow(model: str, system: str, user: str) -> dict[str, Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return {"findings": []}

    for engine in ("langgraph", "asyncio"):
        peak = 0
        services = make_services(make_settings(workflow_engine=engine), llm=ScriptedLLMClient(default=slow))
        await _review(services, open_pull(services))
        assert peak == 4, f"{engine}: specialists did not overlap"


async def test_hallucinated_location_never_reaches_the_pull_request(make_services):
    """Grounding: a finding that cites code which is not in the diff is not published."""
    ghost = llm_finding(
        file_path="app/does_not_exist.py", line_start=3, evidence="eval(request.args['q'])",
        category="command-injection", summary="eval on request input",
    )  # fmt: skip
    wrong_line = llm_finding(line_start=2, line_end=2)  # right evidence, wrong line number
    services = make_services(llm=ScriptedLLMClient({"security": {"findings": [ghost, wrong_line]}}))
    _, review, findings = await _review(services, open_pull(services))

    by_file = {f["file_path"]: f for f in findings if f["agent_type"] == "security"}
    assert "app/does_not_exist.py" not in by_file or by_file["app/does_not_exist.py"]["status"] == "suppressed"
    for review_post in services.github.reviews.get(("acme/shop", 1), []):
        assert all(c["path"] != "app/does_not_exist.py" for c in review_post["comments"])
        assert "does_not_exist" not in review_post["body"]
    # The mislocated finding is snapped to the line its evidence actually sits on.
    real = by_file["app/users.py"]
    assert real["line_start"] == 9 and real["grounded"] is True


async def test_low_confidence_medium_finding_goes_to_a_human(make_services):
    unsure = llm_finding(severity="medium", category="race-condition", confidence=0.45, summary="Possible race on conn")
    services = make_services(llm=ScriptedLLMClient({"quality": {"findings": [unsure]}}))
    _, review, _ = await _review(services, open_pull(services))
    assert review["decision"] == "human_review" and review["status"] == "pending_human"
    assert services.github.reviews == {}
    assert len(await services.store.list_hitl("pending")) == 1


async def test_noise_is_suppressed_not_posted(make_services):
    nit = llm_finding(severity="low", category="naming", confidence=0.2, summary="Rename q", line_start=9)
    services = make_services(llm=ScriptedLLMClient({"quality": {"findings": [nit]}}))
    _, review, findings = await _review(services, open_pull(services, CLEAN_DIFF.replace("app/text.py", "app/users.py")))
    nits = [f for f in findings if f["category"] == "naming"]
    assert all(f["status"] == "suppressed" for f in nits)


async def test_clean_change_stays_silent(make_services):
    services = make_services(llm=ScriptedLLMClient())
    _, review, findings = await _review(services, open_pull(services, CLEAN_DIFF))
    assert review["decision"] == "no_findings"
    assert services.github.reviews == {}  # selectivity: say nothing when there is nothing to say


async def test_a_hung_specialist_cannot_deadlock_the_join(make_services):
    """Every node has a time budget; the review completes with the other three."""

    async def hang(model: str, system: str, user: str) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {"findings": []}

    llm = ScriptedLLMClient({"security": {"findings": [llm_finding()]}, "docs": hang})
    services = make_services(make_settings(agent_timeout_s=0.3, llm_timeout_s=0.2), llm=llm)
    result, review, findings = await asyncio.wait_for(_review(services, open_pull(services)), timeout=10)

    assert review["status"] in ("posted", "pending_human")
    assert review["coverage"] < 1.0  # the gap is reported, not hidden
    assert any(f["category"] == "sql-injection" for f in findings)
    errors = [e for e in services.store.events if e["agent"] == "docs" and e["event_type"] in ("error", "retry")]
    assert errors, "the timeout must be visible on the event spine"


async def test_model_failure_degrades_to_deterministic_rules(make_services):
    """Slower-but-correct: when the model is down, the rule packs still catch the injection."""
    llm = ScriptedLLMClient(default=RetryableError("upstream 503"))
    services = make_services(llm=llm)
    _, review, findings = await _review(services, open_pull(services))
    assert any(f["category"] == "sql-injection" and f["source"] == "rules" for f in findings)
    assert review["coverage"] < 1.0
    assert review["status"] in ("posted", "pending_human")


async def test_unparseable_model_output_is_contained(make_services):
    llm = ScriptedLLMClient({"security": "I think this code looks fine!", "quality": {"findings": "nope"}})
    services = make_services(llm=llm)
    _, review, _ = await _review(services, open_pull(services))
    assert review["status"] in ("posted", "pending_human", "clean")


async def test_same_commit_is_reviewed_once(make_services):
    services = make_services(llm=ScriptedLLMClient({"security": {"findings": [llm_finding()]}}))
    job = open_pull(services)
    first = await jobs.review_pull_request(services, job)
    second = await jobs.review_pull_request(services, job)
    assert second["review_id"] == first["review_id"] and "skipped" in second
    assert len(services.github.reviews[("acme/shop", 1)]) == 1


async def test_new_commit_supersedes_the_waiting_review(make_services):
    unsure = llm_finding(severity="medium", category="race-condition", confidence=0.45)
    services = make_services(llm=ScriptedLLMClient({"quality": {"findings": [unsure]}}))
    old = await jobs.review_pull_request(services, open_pull(services, sha="1" * 40))
    assert len(await services.store.list_hitl("pending")) == 1
    await jobs.review_pull_request(services, open_pull(services, sha="2" * 40))
    old_review = await services.store.get_review(old["review_id"])
    assert old_review["status"] == "superseded"
    pending = await services.store.list_hitl("pending")
    assert all(item["review_id"] != old["review_id"] for item in pending)


async def test_stale_commit_is_not_reviewed(make_services):
    """The job sat in the queue while the author pushed again: skip it."""
    services = make_services(llm=ScriptedLLMClient())
    job = open_pull(services, sha="3" * 40)
    services.github.add_pull("acme/shop", 1, "4" * 40, [], title="moved on")
    result = await jobs.review_pull_request(services, job)
    assert result["status"] == "superseded"
    assert services.github.reviews == {}


async def test_every_step_is_on_the_event_spine(make_services):
    services = make_services(llm=ScriptedLLMClient({"security": {"findings": [llm_finding()]}}))
    result, review, _ = await _review(services, open_pull(services))
    trace = await services.store.get_trace(result["review_id"])
    kinds = {e["event_type"] for e in trace}
    assert {"span.start", "span.end", "llm.call", "decision"} <= kinds
    llm_calls = [e for e in trace if e["event_type"] == "llm.call"]
    assert len(llm_calls) == 4 and all(e["tokens_in"] > 0 and e["model"] == "scripted" for e in llm_calls)
    assert review["cost_usd"] == pytest.approx(sum(e["cost_usd"] for e in llm_calls), rel=1e-6)
    spans = {e["span_id"] for e in trace if e["event_type"] == "span.start"}
    ended = {e["span_id"] for e in trace if e["event_type"] == "span.end"}
    assert spans == ended, "every span that starts must end"


async def test_secrets_in_the_diff_are_masked_before_the_model_sees_them(make_services):
    from backend.evaluation.golden_dataset import get_case

    case = get_case("hardcoded-secret")
    secret = case.must_not_leak[0]
    llm = ScriptedLLMClient()
    services = make_services(llm=llm)
    result, review, findings = await _review(services, open_pull(services, case.diff))
    assert all(secret not in call["user"] for call in llm.calls)
    assert all(secret not in str(f) for f in findings)
    assert all(secret not in str(e) for e in services.store.events)
    assert review["decision"] == "escalate"


async def test_prompt_injection_in_the_diff_is_data_not_instruction(make_services):
    from backend.evaluation.golden_dataset import get_case

    case = get_case("prompt-injection-in-diff")
    llm = ScriptedLLMClient()  # a compromised model would "approve"; the gate does not rely on it
    services = make_services(llm=llm)
    _, review, findings = await _review(services, open_pull(services, case.diff))
    assert review["decision"] in ("human_review", "escalate")
    assert any(f["category"] == "prompt-injection" for f in findings)
    # The untrusted diff is fenced with a per-request boundary the author cannot guess.
    assert "UNTRUSTED" in llm.calls[0]["user"].upper()


async def test_budget_exhaustion_falls_back_to_rules(make_services):
    services = make_services(make_settings(daily_budget_usd=0.01), llm=ScriptedLLMClient({"security": {"findings": [llm_finding()]}}))
    await services.emitter.emit("llm.call", agent="security", model="scripted", cost_usd=0.5, tokens_in=1, tokens_out=1)
    llm = services.llm
    _, review, findings = await _review(services, open_pull(services))
    assert llm.calls == [], "no model call may be made once the daily budget is spent"
    assert any(f["source"] == "rules" for f in findings)


async def test_a_model_cannot_talk_a_deterministic_critical_down(make_services):
    """The guard found a live credential. The model calls it a 'low' nit. Critical wins."""
    from backend.evaluation.golden_dataset import get_case
    from backend.security.secret_masking import mask

    case = get_case("hardcoded-secret")
    masked_line = mask(next(line[1:] for line in case.diff.splitlines() if "stripe.api_key" in line)).strip()
    downplayed = llm_finding(
        severity="low", category="hardcoded-secret", summary="Maybe move this value to config some day",
        file_path="app/payments.py", line_start=4, line_end=4, evidence=masked_line, confidence=0.35,
        rationale="Probably a test key.", suggestion="Load it from the environment.",
    )  # fmt: skip
    services = make_services(llm=ScriptedLLMClient({"security": {"findings": [downplayed]}}))
    _, review, findings = await _review(services, open_pull(services, case.diff))
    secret = next(f for f in findings if f["category"] == "hardcoded-secret")
    assert secret["severity"] == "critical" and secret["confidence"] >= 0.9 and secret["grounded"] is True
    assert review["decision"] == "escalate"
    assert sum(1 for f in findings if f["category"] == "hardcoded-secret") == 1  # merged, not duplicated


async def test_evidence_quoted_from_the_masked_diff_is_still_grounded(make_services):
    from backend.security.secret_masking import mask

    diff = SQLI_DIFF.replace('conn = sqlite3.connect("app.db")', 'conn = connect(password="hunter2-S3cr3t-Passw0rd!")')
    raw = 'conn = connect(password="hunter2-S3cr3t-Passw0rd!")'
    quoted = mask(raw)
    assert quoted != raw, "precondition: the line the model saw was masked"
    f = llm_finding(category="insecure-default", severity="medium", line_start=4, line_end=4, evidence=quoted,
                    summary="Connection opened at import time", confidence=0.8)
    services = make_services(llm=ScriptedLLMClient({"quality": {"findings": [f]}}))
    _, _, findings = await _review(services, open_pull(services, diff))
    got = next(x for x in findings if x["category"] == "insecure-default")
    assert got["grounded"] is True and not any("not found" in a for a in got["adjustments"])
