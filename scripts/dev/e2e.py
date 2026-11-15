"""End-to-end drill with real processes: API + ARQ worker + Postgres + Redis.

    TIGER_DATABASE_URL=postgresql://... REDIS_URL=redis://... python -m scripts.dev.e2e

GitHub and the model are replaced by ``scripts.dev.fake_services``; everything
else is the production code path: a signed webhook is posted to the API, the
job travels through Redis to a separate worker process, the review is written
to Postgres and "posted" to the fake GitHub over HTTP.

Scenarios (all run by default, or pick with --only):

    ingress     unsigned / tampered / duplicate deliveries are refused or dropped
    review      webhook -> queue -> four specialists -> gate -> posted review
    hitl        an uncertain review waits; approving it through the API publishes it
    resume      the worker is stopped mid-review; a new worker resumes from the
                Redis checkpoint without paying for the finished specialists again
    degrade     the model API fails for one agent; the review still completes

Exit code 0 means every expectation held.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from backend.cli import sample_payload
from backend.evaluation.golden_dataset import get_case
from backend.webhook_receiver.signature import SIGNATURE_HEADER, compute_signature

ROOT = Path(__file__).resolve().parents[2]
SECRET = "e2e-webhook-secret"
API, FAKE = "http://127.0.0.1:8010", "http://127.0.0.1:9110"
TOKENS = "e2e-admin:admin:root,e2e-rev:reviewer:alice"
AUTH = {"Authorization": "Bearer e2e-rev"}


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        ENVIRONMENT="development", LOG_LEVEL="WARNING", GITHUB_WEBHOOK_SECRET=SECRET,
        GITHUB_API_URL=FAKE, GITHUB_TOKEN="fake-token", GITHUB_APP_ID="", GITHUB_PRIVATE_KEY="",
        LLM_PROVIDER="openai", OPENAI_API_KEY="fake-key", OPENAI_BASE_URL=f"{FAKE}/llm", ANTHROPIC_API_KEY="",
        EMBEDDING_PROVIDER="openai", MODEL_SECURITY="scripted", MODEL_QUALITY="scripted", MODEL_TESTS="scripted",
        MODEL_DOCS="scripted", MODEL_FALLBACK="scripted", API_TOKENS=TOKENS, SLACK_WEBHOOK_URL="",
        AGENT_TIMEOUT_S="20", LLM_TIMEOUT_S="15", RETRY_BASE_DELAY_S="0.05", PYTHONUNBUFFERED="1",
    )  # fmt: skip
    env.update(extra)
    return env


class Procs:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.procs: dict[str, subprocess.Popen[bytes]] = {}

    def start(self, name: str, *cmd: str, **env: str) -> None:
        log = open(self.log_dir / f"{name}.log", "ab")  # noqa: SIM115 - lives as long as the process
        self.procs[name] = subprocess.Popen([sys.executable, "-m", *cmd], cwd=ROOT, env=_env(**env), stdout=log, stderr=log)

    def stop(self, name: str, sig: int = signal.SIGTERM) -> None:
        proc = self.procs.pop(name, None)
        if proc and proc.poll() is None:
            proc.send_signal(sig)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=15)
            if proc.poll() is None:
                proc.kill()

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.stop(name)


async def _wait_http(client: httpx.AsyncClient, url: str, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if (await client.get(url)).status_code < 500:
                return
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{url} did not come up within {timeout}s")


async def _webhook(client: httpx.AsyncClient, payload: dict[str, Any], *, delivery: str | None = None, sign: str = "good") -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery or str(uuid.uuid4())}
    if sign == "good":
        headers[SIGNATURE_HEADER] = compute_signature(SECRET, body)
    elif sign == "bad":
        headers[SIGNATURE_HEADER] = compute_signature("wrong-secret", body)
    return await client.post(f"{API}/webhooks/github", content=body, headers=headers)


async def _open_pr(client: httpx.AsyncClient, case_id: str, number: int) -> tuple[dict[str, Any], str]:
    case = get_case(case_id)
    sha = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    repo = "acme/shop"
    r = await client.post(f"{FAKE}/_admin/pulls", json={"repo": repo, "number": number, "sha": sha, "diff": case.diff, "title": case.title})
    r.raise_for_status()
    return sample_payload(repo, number, sha), sha


async def _review_for(client: httpx.AsyncClient, sha: str, *, until: set[str], timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        reviews = (await client.get(f"{API}/api/reviews", headers=AUTH, params={"limit": 100})).json()["reviews"]
        last = next((r for r in reviews if r["head_sha"] == sha), {})
        if last.get("status") in until:
            return last
        await asyncio.sleep(0.4)
    raise RuntimeError(f"review for {sha[:7]} did not reach {sorted(until)} (last: {last.get('status')})")


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def __call__(self, ok: bool, what: str) -> None:
        print(f"   {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            self.failures.append(what)


async def scenario_ingress(client: httpx.AsyncClient, check: Check) -> None:
    payload, _ = await _open_pr(client, "clean-documented-change", 11)
    check((await _webhook(client, payload, sign="none")).status_code == 401, "unsigned webhook -> 401")
    check((await _webhook(client, payload, sign="bad")).status_code == 401, "tampered signature -> 401")
    first = await _webhook(client, payload, delivery="e2e-dup")
    second = await _webhook(client, payload, delivery="e2e-dup")
    check(first.status_code == 202, "signed webhook -> 202 queued")
    check(second.status_code == 200 and second.json()["status"] == "duplicate", "same delivery id again -> dropped as duplicate")
    check(first.elapsed.total_seconds() < 2.0, f"acknowledged in {first.elapsed.total_seconds() * 1000:.0f} ms (GitHub allows 10 s)")


async def scenario_review(client: httpx.AsyncClient, check: Check) -> None:
    payload, sha = await _open_pr(client, "sql-injection-fstring", 12)
    await _webhook(client, payload)
    review = await _review_for(client, sha, until={"posted", "pending_human", "escalated", "clean", "failed"})
    check(review["status"] == "posted", f"review auto-posted (status={review['status']}, confidence={review.get('overall_confidence')})")
    state = (await client.get(f"{FAKE}/_admin/state")).json()
    posted = state["reviews"].get("acme/shop#12", [])
    check(len(posted) == 1, "exactly one review on the pull request")
    check(bool(posted) and any("sql-injection" in c["body"] for c in posted[0]["comments"]), "inline comment on the injected line")
    trace = (await client.get(f"{API}/api/reviews/{review['id']}/trace", headers=AUTH)).json()
    check(trace["totals"]["llm_calls"] == 4, f"four specialist model calls on the event spine ({trace['totals']['llm_calls']})")
    check(trace["totals"]["cost_usd"] > 0, f"cost recorded (${trace['totals']['cost_usd']})")
    eco = (await client.get(f"{API}/api/economics/summary", headers=AUTH)).json()
    check(eco["budget"]["spent_today"] > 0 and len(eco["agents"]) >= 4, "cost rollups readable by the budget guard and dashboard")
    # A redelivery under a new id must not review the same commit twice.
    await _webhook(client, payload)
    await asyncio.sleep(2.0)
    state = (await client.get(f"{FAKE}/_admin/state")).json()
    check(len(state["reviews"].get("acme/shop#12", [])) == 1, "redelivery of the same commit does not post a second review")


async def scenario_hitl(client: httpx.AsyncClient, check: Check) -> None:
    payload, sha = await _open_pr(client, "hardcoded-secret", 13)
    await _webhook(client, payload)
    review = await _review_for(client, sha, until={"posted", "pending_human", "escalated", "clean", "failed"})
    check(review["status"] == "escalated", f"critical finding escalates to a human (status={review['status']})")
    state = (await client.get(f"{FAKE}/_admin/state")).json()
    check("acme/shop#13" not in state["reviews"], "nothing posted before a human decides")
    queue = (await client.get(f"{API}/api/hitl", headers=AUTH)).json()
    item = next((i for i in queue["items"] if i["review_id"] == review["id"]), None)
    check(item is not None and item["kind"] == "escalation", "escalation is at the top of the approval queue")
    if item is None:
        return
    decision = await client.post(f"{API}/api/hitl/{item['id']}/decision", json={"action": "approve"}, headers=AUTH)
    check(decision.status_code == 200, "reviewer approves through the API")
    again = await client.post(f"{API}/api/hitl/{item['id']}/decision", json={"action": "approve"}, headers=AUTH)
    check(again.status_code == 409, "a second decision on the same item -> 409")
    review = await _review_for(client, sha, until={"posted", "resolved", "rejected"})
    detail = (await client.get(f"{API}/api/reviews/{review['id']}", headers=AUTH)).json()
    statuses = {f["category"]: f["status"] for f in detail["findings"]}
    check(statuses.get("hardcoded-secret") == "withheld", "critical security finding withheld from the public thread")
    secret = get_case("hardcoded-secret").must_not_leak[0]
    everything = json.dumps((await client.get(f"{FAKE}/_admin/state")).json()) + json.dumps(detail)
    check(secret not in everything, "the secret value appears nowhere in stored or posted output")


async def scenario_resume(client: httpx.AsyncClient, check: Check, procs: Procs) -> None:
    await client.post(f"{FAKE}/_admin/llm", json={"delay": {"docs": 8}})
    before = len((await client.get(f"{FAKE}/_admin/state")).json()["llm_calls"])
    payload, sha = await _open_pr(client, "command-injection-shell", 14)
    await _webhook(client, payload)
    # Wait until three specialists have answered and the fourth is still thinking.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        calls = (await client.get(f"{FAKE}/_admin/state")).json()["llm_calls"][before:]
        if len(calls) >= 4:
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(1.0)
    print("   ...stopping the worker while the docs specialist is still running")
    procs.stop("worker")
    await client.post(f"{FAKE}/_admin/llm", json={"delay": {}})
    procs.start("worker", "arq", "backend.job_queue.worker.WorkerSettings")
    review = await _review_for(client, sha, until={"posted", "pending_human", "escalated", "clean", "failed"}, timeout=90)
    check(review["status"] in ("posted", "pending_human"), f"review completed after the restart (status={review['status']})")
    calls = [c["agent"] for c in (await client.get(f"{FAKE}/_admin/state")).json()["llm_calls"][before:]]
    repeated = {a: calls.count(a) for a in ("security", "quality", "tests")}
    check(all(n == 1 for n in repeated.values()), f"finished specialists were not run again after resume {repeated}")
    check(calls.count("docs") == 2, f"only the interrupted specialist ran again (docs calls: {calls.count('docs')})")


async def scenario_degrade(client: httpx.AsyncClient, check: Check) -> None:
    await client.post(f"{FAKE}/_admin/llm", json={"fail": {"security": 503}})
    payload, sha = await _open_pr(client, "unsafe-deserialization", 15)
    await _webhook(client, payload)
    review = await _review_for(client, sha, until={"posted", "pending_human", "escalated", "clean", "failed"}, timeout=90)
    await client.post(f"{FAKE}/_admin/llm", json={"fail": {}})
    check(review["status"] in ("posted", "pending_human"), f"review completed while the security model was down (status={review['status']})")
    check(float(review.get("coverage") or 1) < 1.0, f"reduced coverage is reported ({review.get('coverage')})")
    detail = (await client.get(f"{API}/api/reviews/{review['id']}", headers=AUTH)).json()
    check(any(f["category"] == "unsafe-deserialization" and f["source"] == "rules" for f in detail["findings"]),
          "the deterministic rule pack still caught the vulnerability")
    trace = (await client.get(f"{API}/api/reviews/{review['id']}/trace", headers=AUTH)).json()
    check(trace["totals"]["errors"] > 0, "the failures are visible on the event spine")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["ingress", "review", "hitl", "resume", "degrade"], action="append")
    parser.add_argument("--keep-logs", default=".local/e2e-logs")
    args = parser.parse_args()
    if not (os.environ.get("TIGER_DATABASE_URL") or os.environ.get("DATABASE_URL")) or not os.environ.get("REDIS_URL"):
        print("set TIGER_DATABASE_URL and REDIS_URL (see docker-compose.yml: `make infra`)")
        return 2
    log_dir = ROOT / args.keep_logs
    log_dir.mkdir(parents=True, exist_ok=True)
    for old in log_dir.glob("*.log"):
        old.unlink()
    procs = Procs(log_dir)
    check = Check()
    wanted = args.only or ["ingress", "review", "hitl", "resume", "degrade"]
    try:
        procs.start("fake", "uvicorn", "scripts.dev.fake_services:app", "--port", "9110", "--log-level", "warning")
        procs.start("api", "uvicorn", "backend.main:app", "--port", "8010", "--log-level", "warning")
        procs.start("worker", "arq", "backend.job_queue.worker.WorkerSettings")
        async with httpx.AsyncClient(timeout=20.0) as client:
            await _wait_http(client, f"{FAKE}/_admin/state")
            await _wait_http(client, f"{API}/readyz")
            ready = (await client.get(f"{API}/readyz")).json()
            status = (await client.get(f"{API}/api/system/status", headers=AUTH)).json()
            print(f"stack up: database={status['database']} engine={status['engine']} queue={status['queue'].get('backend')} ready={ready['ready']}")
            for name in wanted:
                print(f"\n[{name}]")
                if name == "resume":
                    await scenario_resume(client, check, procs)
                else:
                    await globals()[f"scenario_{name}"](client, check)
    finally:
        procs.stop_all()
    print()
    if check.failures:
        print(f"FAILED: {len(check.failures)} expectation(s) did not hold. Logs: {log_dir}")
        return 1
    print("all expectations held")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
