"""M7: the human approval queue, disputes and the learning loop."""

from __future__ import annotations

import asyncio

import pytest

from backend.evaluation.golden_dataset import get_case
from backend.hitl.dispute import parse_disputes
from backend.hitl.feedback import FeedbackCalibrator
from backend.hitl.queue import DecisionConflict
from backend.job_queue import jobs
from backend.models.schemas import HitlDecisionItem, HitlDecisionRequest
from backend.tools.llm_client import ScriptedLLMClient
from tests.conftest import llm_finding, make_settings, open_pull

UNSURE = llm_finding(severity="medium", category="race-condition", confidence=0.45, summary="Possible race on conn")
SURE = llm_finding()


async def _queued(make_services, findings=(UNSURE, SURE), **settings):
    llm = ScriptedLLMClient({"security": {"findings": [f for f in findings if f["category"] == "sql-injection"]},
                             "quality": {"findings": [f for f in findings if f["category"] != "sql-injection"]}})
    services = make_services(make_settings(**settings), llm=llm)
    result = await jobs.review_pull_request(services, open_pull(services))
    item = (await services.store.list_hitl("pending"))[0]
    return services, result["review_id"], item


async def test_nothing_is_posted_until_a_human_decides(make_services):
    services, review_id, item = await _queued(make_services)
    assert services.github.reviews == {}
    assert (await services.store.get_review(review_id))["status"] == "pending_human"
    assert item["kind"] == "approval" and item["pending_findings"] >= 2


async def test_approval_publishes_only_the_accepted_findings(make_services):
    services, review_id, item = await _queued(make_services)
    findings = await services.store.get_findings(review_id)
    race = next(f for f in findings if f["category"] == "race-condition")
    request = HitlDecisionRequest(action="approve", findings=[HitlDecisionItem(finding_id=race["finding_id"], verdict="rejected", note="conn is module-level and single-threaded")])
    outcome = await services.approvals.decide(item["id"], request, "alice")
    assert outcome["rejected"] == 1 and outcome["accepted"] >= 1

    posted = services.github.reviews[("acme/shop", 1)][0]
    assert "race-condition" not in str(posted)
    assert any("sql-injection" in c["body"] for c in posted["comments"])
    assert (await services.store.get_review(review_id))["status"] == "posted"
    statuses = {f["category"]: f["status"] for f in await services.store.get_findings(review_id)}
    assert statuses["race-condition"] == "rejected" and statuses["sql-injection"] == "posted"
    # Both verdicts became feedback: the learning loop's input.
    verdicts = {(f["category"], f["verdict"]) for f in services.store.feedback}
    assert ("race-condition", "rejected") in verdicts and ("sql-injection", "accepted") in verdicts


async def test_rejecting_everything_posts_nothing(make_services):
    services, review_id, item = await _queued(make_services)
    outcome = await services.approvals.decide(item["id"], HitlDecisionRequest(action="reject", note="not useful"), "bob")
    assert outcome["accepted"] == 0 and outcome["publishing"] is False
    assert services.github.reviews == {}
    assert (await services.store.get_review(review_id))["status"] == "rejected"


async def test_two_reviewers_cannot_decide_the_same_item(make_services):
    services, _, item = await _queued(make_services)
    request = HitlDecisionRequest(action="approve")
    results = await asyncio.gather(
        services.approvals.decide(item["id"], request, "alice"),
        services.approvals.decide(item["id"], request, "bob"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, DecisionConflict) for r in results) == 1
    assert len(services.github.reviews[("acme/shop", 1)]) == 1  # posted exactly once


async def test_critical_always_escalates_and_is_withheld_from_a_public_pr(make_services):
    case = get_case("hardcoded-secret")
    services = make_services(llm=ScriptedLLMClient())
    result = await jobs.review_pull_request(services, open_pull(services, case.diff))
    review = await services.store.get_review(result["review_id"])
    assert review["decision"] == "escalate" and review["status"] == "escalated"
    assert services.alerter.sent and services.alerter.sent[-1]["severity"] == "critical"
    item = (await services.store.list_hitl("pending"))[0]
    assert item["kind"] == "escalation"

    outcome = await services.approvals.decide(item["id"], HitlDecisionRequest(action="approve"), "alice")
    assert outcome["accepted"] >= 1
    # Public pull request: the vulnerability is handled privately. Even "something was
    # reported privately" would be a hint, so nothing about it reaches the thread.
    secret = case.must_not_leak[0]
    public = str(services.github.reviews) + str(services.github.comments)
    assert "hardcoded-secret" not in public and secret not in public
    statuses = {f["category"]: f["status"] for f in await services.store.get_findings(result["review_id"])}
    assert statuses["hardcoded-secret"] == "withheld"


async def test_a_maintainer_can_choose_to_publish_a_withheld_finding(make_services):
    case = get_case("hardcoded-secret")
    services = make_services(llm=ScriptedLLMClient())
    await jobs.review_pull_request(services, open_pull(services, case.diff))
    item = (await services.store.list_hitl("pending"))[0]
    await services.approvals.decide(item["id"], HitlDecisionRequest(action="approve", publish_withheld=True), "alice")
    posted = services.github.reviews[("acme/shop", 1)][0]
    assert "hardcoded-secret" in str(posted["comments"])
    assert case.must_not_leak[0] not in str(posted)  # the value itself is still masked


async def test_human_approves_all_mode_never_auto_posts(make_services):
    services, review_id, item = await _queued(make_services, findings=(SURE,), autonomy_level="human_approves_all")
    assert services.github.reviews == {}
    assert item["kind"] == "approval"


async def test_shadow_mode_records_and_posts_nothing(make_services):
    llm = ScriptedLLMClient({"security": {"findings": [SURE]}})
    services = make_services(make_settings(autonomy_level="shadow"), llm=llm)
    result = await jobs.review_pull_request(services, open_pull(services))
    review = await services.store.get_review(result["review_id"])
    assert review["decision"] == "shadow" and services.github.reviews == {}
    assert await services.store.list_hitl("pending") == []
    assert await services.store.get_findings(result["review_id"])


def test_dispute_command_parsing():
    assert parse_disputes("/dispute F-1a2b3c4d this is a test fixture") == [("F-1a2b3c4d", "this is a test fixture")]
    assert parse_disputes("thanks!\n/dispute F-deadbeef") == [("F-deadbeef", "")]
    assert parse_disputes("I dispute this") == []


async def test_developer_dispute_opens_a_second_opinion(make_services):
    llm = ScriptedLLMClient({"security": {"findings": [SURE]}})
    services = make_services(llm=llm)
    result = await jobs.review_pull_request(services, open_pull(services))
    finding = next(f for f in await services.store.get_findings(result["review_id"]) if f["category"] == "sql-injection")
    assert finding["status"] == "posted"

    out = await jobs.handle_dispute(services, {
        "repo": "acme/shop", "pr_number": 1, "finding_id": finding["finding_id"],
        "reason": "user_id is validated upstream", "actor": "octocat", "installation_id": None,
    })
    assert out["status"] == "disputed"
    item = (await services.store.list_hitl("pending"))[0]
    assert item["kind"] == "dispute" and item["finding_id"] == finding["finding_id"]
    assert any(f["verdict"] == "disputed" for f in services.store.feedback)
    assert services.github.comments[("acme/shop", 1)], "the developer gets an acknowledgement"

    # A maintainer sides with the developer: the finding is retracted and counted as a rejection.
    await services.approvals.decide(item["id"], HitlDecisionRequest(action="reject", note="validated in middleware"), "alice")
    after = next(f for f in await services.store.get_findings(result["review_id"]) if f["finding_id"] == finding["finding_id"])
    assert after["status"] == "retracted"
    assert ("sql-injection", "rejected") in {(f["category"], f["verdict"]) for f in services.store.feedback}


async def test_dispute_webhook_comment_is_routed_to_the_handler(make_services):
    """A `/dispute F-xxxx reason` comment on the pull request becomes a dispute job."""
    from backend.webhook_receiver.parser import parse_event

    services = make_services()
    payload = {
        "action": "created",
        "issue": {"number": 1, "pull_request": {"url": "x"}},
        "comment": {"id": 5, "body": "/dispute F-1a2b3c4d validated upstream", "user": {"login": "octocat", "type": "User"}},
        "repository": {"full_name": "acme/shop"},
    }
    action = parse_event("issue_comment", payload, "d-9", services.settings)
    assert action.kind == "dispute" and action.job == "handle_dispute"
    assert action.args[0]["finding_id"] == "F-1a2b3c4d" and action.args[0]["actor"] == "octocat"


async def test_feedback_needs_evidence_from_several_people_before_it_counts(services):
    """Adversarial feedback: one account rejecting everything must not blind the reviewer."""
    calibrator = FeedbackCalibrator(services.store, min_evidence=5, min_actors=2, half_life_days=30)

    async def reject(actor: str, n: int) -> None:
        for i in range(n):
            await services.store.add_feedback(
                review_id=f"r{actor}{i}", finding_id=f"F-{actor}{i}", repo="acme/shop", agent_type="security",
                category="sql-injection", verdict="rejected", actor=actor, source="hitl", note="",
            )  # fmt: skip

    await reject("mallory", 3)
    assert await calibrator.multipliers("acme/shop") == {}  # too little evidence
    await reject("mallory", 20)
    assert await calibrator.multipliers("acme/shop") == {}  # plenty of verdicts, but one voice
    await reject("alice", 3)
    multipliers = await calibrator.multipliers("acme/shop")
    assert multipliers[("security", "sql-injection")] < 1.0  # now it is a signal
    assert multipliers[("security", "sql-injection")] >= 0.5  # bounded: never silenced outright


async def test_learned_calibration_lowers_confidence_of_a_repeatedly_rejected_category(make_services):
    services = make_services(llm=ScriptedLLMClient({"docs": {"findings": [llm_finding(
        severity="low", category="missing-docstring", confidence=0.7, summary="get_user lacks detail", line_start=7, line_end=7,
        evidence="def get_user(user_id):")]}}))
    for i in range(6):
        await services.store.add_feedback(
            review_id=f"r{i}", finding_id=f"F-{i}", repo="acme/shop", agent_type="docs", category="missing-docstring",
            verdict="rejected", actor="alice" if i % 2 else "bob", source="hitl", note="",
        )  # fmt: skip
    result = await jobs.review_pull_request(services, open_pull(services))
    doc = next(f for f in await services.store.get_findings(result["review_id"]) if f["category"] == "missing-docstring")
    assert doc["confidence"] < 0.7
    assert any("feedback" in a or "calibrat" in a for a in doc["adjustments"])


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_decisions_are_audited(make_services, action):
    services, review_id, item = await _queued(make_services)
    await services.approvals.decide(item["id"], HitlDecisionRequest(action=action), "alice")
    audits = [e for e in services.store.events if e["event_type"] == "audit"]
    assert audits and audits[-1]["payload"]["actor"] == "alice"
