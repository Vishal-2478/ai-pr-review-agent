"""Deterministic core: diff parsing, grounding, aggregation, the gate, evaluation, model clients."""

from __future__ import annotations

import json

import httpx
import pytest

from backend.agents.aggregator import aggregate, same_issue
from backend.agents.contracts import AgentResult, Finding
from backend.agents.grounding import ground_findings
from backend.config import Settings
from backend.core.exceptions import PermanentError, RetryableError
from backend.evaluation.golden_dataset import GOLDEN_CASES, get_case
from backend.evaluation.judge import match_case
from backend.evaluation.regression_gate import check_against_baseline, load_baseline, run_eval
from backend.hitl.gate import apply_gate
from backend.integrations.diff import parse_patch, parse_unified_diff, render_diff
from backend.models.enums import AgentType, GateDecision, Severity
from backend.models.schemas import RetrievedContext
from backend.tools.llm_client import AnthropicClient, extract_json
from tests.conftest import SQLI_DIFF, make_settings


def finding(**over) -> Finding:
    base = {
        "agent": AgentType.SECURITY, "severity": Severity.HIGH, "category": "sql-injection",
        "summary": "User input is interpolated into SQL", "file_path": "app/users.py", "line_start": 9, "line_end": 9,
        "confidence": 0.8, "rationale": "user_id reaches the query", "evidence": 'query = f"SELECT * FROM users WHERE id = {user_id}"',
    }  # fmt: skip
    if "agent_type" in over:
        over["agent"] = over.pop("agent_type")
    base.update(over)
    return Finding.build(**base)


def result(agent: AgentType, findings: list[Finding], status: str = "ok") -> AgentResult:
    return AgentResult(agent=agent, status=status, findings=findings)


# ----------------------------------------------------------------------- diff
def test_patch_line_numbers_follow_the_new_file():
    patch = "@@ -10,4 +10,6 @@ def f():\n context a\n-removed\n+added one\n+added two\n context b\n+added three\n"
    parsed = parse_patch(patch)
    assert parsed.text_at(11) == "added one" and parsed.text_at(12) == "added two" and parsed.text_at(14) == "added three"
    assert parsed.commentable >= {10, 11, 12, 13, 14}
    assert parsed.text_at(99) is None


def test_unified_diff_handles_new_deleted_and_renamed_files():
    files = parse_unified_diff(SQLI_DIFF + "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n")
    by_path = {f.path: f for f in files}
    assert by_path["app/users.py"].status == "added" and by_path["app/users.py"].additions == 10
    assert by_path["old.py"].status == "removed"


def test_diff_rendering_respects_the_budget_and_says_what_was_cut():
    big = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1,1 +1,40 @@\n" + "".join(f"+line {j} of file {i}\n" for j in range(40))
        for i in range(30)
    )
    text, omitted = render_diff(parse_unified_diff(big), max_chars=4000)
    assert len(text) <= 5000 and omitted, "an over-budget diff must be truncated and the omissions reported"


# ------------------------------------------------------------------ grounding
def test_grounding_snaps_lines_and_penalises_what_it_cannot_verify():
    files = parse_unified_diff(SQLI_DIFF)
    exact, off, invented = finding(), finding(line_start=3, line_end=3), finding(file_path="ghost.py", evidence="os.system(cmd)", category="command-injection")
    grounded = {f.file_path + str(f.line_start): f for f in ground_findings([exact, off, invented], files, RetrievedContext())}
    assert grounded["app/users.py9"].grounded and grounded["app/users.py9"].confidence >= 0.8
    assert all(f.line_start == 9 for f in grounded.values() if f.file_path == "app/users.py")  # snapped to the evidence
    ghost = next((f for f in grounded.values() if f.file_path == "ghost.py"), None)
    assert ghost is None or (not ghost.grounded and ghost.confidence < 0.3)  # under the noise floor: never surfaced


# ----------------------------------------------------------------- aggregator
def test_same_issue_from_two_agents_is_merged_and_corroborated():
    a = finding(confidence=0.7)
    b = finding(agent_type=AgentType.QUALITY, summary="SQL built with an f-string from user input", confidence=0.6, line_start=10, line_end=10)
    assert same_issue(a, b)
    agg = aggregate([result(AgentType.SECURITY, [a]), result(AgentType.QUALITY, [b]), result(AgentType.TESTS, []), result(AgentType.DOCS, [])])
    merged = [f for f in agg.findings if f.category == "sql-injection"]
    assert len(merged) == 1
    assert merged[0].confidence > 0.7 and AgentType.QUALITY.value in merged[0].corroborated_by
    assert agg.coverage == 1.0


def test_different_concerns_on_the_same_line_are_kept_apart():
    sec = finding()
    doc = finding(agent_type=AgentType.DOCS, severity=Severity.INFO, category="missing-docstring", summary="get_user has no docstring")
    agg = aggregate([result(AgentType.SECURITY, [sec]), result(AgentType.DOCS, [doc])])
    assert {f.category for f in agg.findings} == {"sql-injection", "missing-docstring"}


def test_failed_agents_reduce_coverage():
    agg = aggregate([result(AgentType.SECURITY, [], "ok"), result(AgentType.QUALITY, [], "timeout"), result(AgentType.TESTS, [], "ok"), result(AgentType.DOCS, [], "ok")])
    assert agg.coverage < 1.0


def test_feedback_multipliers_recalibrate_confidence():
    agg = aggregate([result(AgentType.SECURITY, [finding(confidence=0.8)])], multipliers={("security", "sql-injection"): 0.5})
    assert agg.findings[0].confidence == pytest.approx(0.4, abs=0.02)
    assert agg.findings[0].adjustments


# ----------------------------------------------------------------------- gate
@pytest.mark.parametrize(
    "findings,coverage,settings_over,injection,expected",
    [
        ([], 1.0, {}, False, GateDecision.NO_FINDINGS),
        ([finding(confidence=0.9)], 1.0, {}, False, GateDecision.AUTO_POST),
        ([finding(severity=Severity.CRITICAL, confidence=0.99)], 1.0, {}, False, GateDecision.ESCALATE),
        ([finding(severity=Severity.CRITICAL, confidence=0.35)], 1.0, {}, False, GateDecision.ESCALATE),  # regardless of confidence
        ([finding(severity=Severity.MEDIUM, confidence=0.5)], 1.0, {}, False, GateDecision.HUMAN_REVIEW),
        ([finding(severity=Severity.LOW, confidence=0.5)], 1.0, {}, False, GateDecision.NO_FINDINGS),  # low severity and unsure = noise
        ([finding(confidence=0.9)], 1.0, {}, True, GateDecision.HUMAN_REVIEW),  # injection attempt
        ([finding(confidence=0.9)], 1.0, {"autonomy_level": "human_approves_all"}, False, GateDecision.HUMAN_REVIEW),
        ([finding(confidence=0.9)], 1.0, {"autonomy_level": "shadow"}, False, GateDecision.SHADOW),
        ([finding(confidence=0.2)], 1.0, {}, False, GateDecision.NO_FINDINGS),  # below the noise floor
        ([finding(confidence=0.65)], 0.5, {}, False, GateDecision.HUMAN_REVIEW),  # half the specialists failed
    ],
)
def test_gate_decision_table(findings, coverage, settings_over, injection, expected):
    outcome = apply_gate(findings, coverage, make_settings(**settings_over), injection_detected=injection)
    assert outcome.decision is expected, outcome.reasons
    assert outcome.reasons, "every decision explains itself"


def test_gate_surfaces_the_most_important_findings_first():
    many = [finding(line_start=i, line_end=i, severity=Severity.LOW if i > 3 else Severity.HIGH, confidence=0.9, summary=f"issue {i}") for i in range(1, 31)]
    outcome = apply_gate(many, 1.0, make_settings(max_inline_comments=5))
    assert outcome.decision is GateDecision.AUTO_POST
    by_id = {f.id: f for f in many}
    first = [by_id[i].severity for i in outcome.to_post[:3]]
    assert first == [Severity.HIGH] * 3  # high severity leads; the publisher caps inline comments


# ----------------------------------------------------------------- evaluation
async def test_golden_set_passes_the_regression_gate_in_rules_mode():
    report = await run_eval(Settings(_env_file=None), use_configured_llm=False)
    assert report.mode == "rules"
    assert report.summary.cases == len(GOLDEN_CASES) >= 15
    assert report.summary.leaks == 0 and report.summary.forbidden_hits == 0
    assert check_against_baseline(report, load_baseline()) == [], report.summary.failed


def test_regression_gate_blocks_a_worse_reviewer():
    case = get_case("sql-injection-fstring")
    score = match_case(case, [], "no_findings")
    assert not score.passed and score.missed

    class Report:
        mode = "rules"

        class summary:  # noqa: N801
            recall, forbidden_hits, decision_accuracy, leaks = 0.5, 2, 0.6, 0
            recall_by_agent = {"security": 0.4}

    problems = check_against_baseline(Report(), load_baseline())  # type: ignore[arg-type]
    assert len(problems) >= 3 and any("recall" in p for p in problems)


def test_a_suppressed_finding_does_not_count_as_caught():
    case = get_case("sql-injection-fstring")
    f = {"file_path": "app/users.py", "agent_type": "security", "category": "sql-injection", "severity": "high", "line_start": 7, "line_end": 7, "status": "suppressed"}
    assert match_case(case, [f], "no_findings").matched == 0
    assert match_case(case, [{**f, "status": "posted"}], "auto_post").matched == 1


# -------------------------------------------------------------- model clients
@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"findings": []}', {"findings": []}),
        ('```json\n{"findings": [{"a": 1}]}\n```', {"findings": [{"a": 1}]}),
        ('Here is my review:\n{"findings": []}\nHope that helps!', {"findings": []}),
    ],
)
def test_extract_json_tolerates_fences_and_chatter(text, expected):
    assert extract_json(text) == expected


def test_extract_json_rejects_prose():
    with pytest.raises(Exception):  # noqa: B017 - any parse failure is contained by the agent
        extract_json("Looks good to me!")


async def test_anthropic_client_speaks_the_messages_api():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": '{"findings": []}'}],
            "usage": {"input_tokens": 120, "output_tokens": 8}, "stop_reason": "end_turn",
        })  # fmt: skip

    http = httpx.AsyncClient(base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler), headers={"x-api-key": "k", "anthropic-version": "2023-06-01"})
    client = AnthropicClient("k", client=http)
    resp = await client.complete_json(model="claude-sonnet-5-5", system="sys", user="usr", max_output_tokens=500, timeout=5, tag="security")
    assert seen["url"].endswith("/v1/messages") and seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"] == {"model": "claude-sonnet-5-5", "max_tokens": 500, "system": "sys", "messages": [{"role": "user", "content": "usr"}]}
    assert (resp.tokens_in, resp.tokens_out, resp.text) == (120, 8, '{"findings": []}')


@pytest.mark.parametrize("status,error", [(429, RetryableError), (529, RetryableError), (500, RetryableError), (400, PermanentError), (401, PermanentError)])
async def test_anthropic_client_classifies_errors(status, error):
    http = httpx.AsyncClient(base_url="https://api.anthropic.com", transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": {"message": "x"}})))
    with pytest.raises(error):
        await AnthropicClient("k", client=http).complete_json(model="m", system="s", user="u", max_output_tokens=10, timeout=5)


async def test_anthropic_client_treats_timeouts_as_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    http = httpx.AsyncClient(base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler))
    with pytest.raises(RetryableError):
        await AnthropicClient("k", client=http).complete_json(model="m", system="s", user="u", max_output_tokens=10, timeout=5)


def test_provider_defaults_and_overrides():
    anthropic = Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="k")
    assert anthropic.model_for("security") == "claude-opus-5-5" and anthropic.model_for("docs") == "claude-haiku-5-5"
    openai = Settings(_env_file=None, llm_provider="openai", openai_api_key="k", model_docs="my-cheap-model")
    assert openai.model_for("security") == "gpt-6.1-sol" and openai.model_for("docs") == "my-cheap-model"
    auto = Settings(_env_file=None, llm_provider="auto", anthropic_api_key="", openai_api_key="")
    assert auto.resolved_llm_provider == "rules"
    principals = Settings(_env_file=None, api_tokens="t1:admin:root, t2:reviewer").token_principals
    assert principals == {"t1": ("admin", "root"), "t2": ("reviewer", "reviewer")}
