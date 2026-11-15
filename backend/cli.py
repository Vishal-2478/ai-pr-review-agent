"""Command line: ``python -m backend.cli <command>`` (or ``pr-review <command>`` once installed).

    demo            review a sample (or your own) diff end to end, fully offline
    eval            run the golden set and the regression gate
    migrate         create or upgrade the database schema
    index-local     build the semantic memory of a local checkout
    send-webhook    send a signed (or deliberately unsigned) GitHub webhook
    sample-payload  print a pull_request webhook payload to play with
    doctor          show the resolved configuration and check connectivity
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from backend import __version__
from backend.config import Settings, get_settings


def _print(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _rule(title: str) -> None:
    _print(f"\n== {title} " + "=" * max(4, 70 - len(title)))


# --------------------------------------------------------------------------- demo
async def _demo(args: argparse.Namespace) -> int:
    """The whole pipeline with nothing to install: in-memory store, fake GitHub."""
    from backend.container import assemble, build_llm
    from backend.database.memory_store import InMemoryStore
    from backend.evaluation.golden_dataset import GOLDEN_CASES, get_case
    from backend.integrations.diff import parse_unified_diff
    from backend.integrations.fake_github import FakeGitHubClient
    from backend.job_queue import jobs
    from backend.models.schemas import HitlDecisionRequest, PRContext
    from backend.observability.alerting import Alerter

    settings = get_settings()
    if args.rules:
        settings = settings.model_copy(update={"llm_provider": "rules"})
    if args.diff:
        diff_text = sys.stdin.read() if args.diff == "-" else Path(args.diff).read_text()
        title, body = args.title or Path(args.diff).name, ""
    else:
        try:
            case = get_case(args.case)
        except KeyError:
            _print(f"unknown case '{args.case}'. Available: " + ", ".join(c.id for c in GOLDEN_CASES))
            return 2
        diff_text, title, body = case.diff, case.title, case.description
    files = parse_unified_diff(diff_text)
    if not files:
        _print("no file changes found in that diff")
        return 2

    store, github = InMemoryStore(), FakeGitHubClient()
    services = assemble(settings, store=store, github=github, llm=build_llm(settings), alerter=Alerter(""))
    repo = "local/demo"
    if args.repo_dir:
        report = await services.indexer.index_local(repo, Path(args.repo_dir))
        _print(f"indexed {report.indexed} files into {report.chunks} chunks ({report.conventions} conventions)")
    pr = PRContext(repo=repo, pr_number=1, head_sha=uuid.uuid4().hex[:12], title=title, body=body, author="you")
    github.add_pull(repo, 1, pr.head_sha, files, title=title, body=body)

    result = await jobs.review_pull_request(services, {"pr": pr.model_dump(), "files": [f.model_dump() for f in files]})
    review_id = result["review_id"]
    review = await store.get_review(review_id) or {}
    findings = await store.get_findings(review_id)
    trace = await store.get_trace(review_id)

    if args.json:
        _print(json.dumps({"review": review, "findings": findings, "events": len(trace)}, indent=2, default=str))
        return 0

    _rule(f"Review of \"{title}\"")
    _print(f"engine: {services.engine.name}    reviewer: {settings.resolved_llm_provider}"
           f"{'' if settings.resolved_llm_provider != 'rules' else ' (deterministic rule packs, no API key needed)'}")
    _print(f"decision: {review.get('decision')}    status: {review.get('status')}    "
           f"overall confidence: {review.get('overall_confidence')}    coverage: {review.get('coverage')}")
    for reason in review.get("reasons") or []:
        _print(f"  - {reason}")

    _rule("Findings (the unit that flows through the system)")
    if not findings:
        _print("none: nothing met the bar for a senior engineer's attention")
    for f in findings:
        _print(f"[{f['severity'].upper():8}] {f['agent_type']:8} {f['file_path']}:{f['line_start']}  "
               f"conf {f['confidence']:.2f}  {f['status']}")
        _print(f"    {f['finding_id']} {f['category']}: {f['summary']}")
        if f.get("evidence"):
            _print(f"    evidence: {f['evidence'].splitlines()[0][:110]}")
        for note in f.get("adjustments") or []:
            _print(f"    ~ {note}")

    pending = await store.list_hitl("pending")
    if pending:
        _rule("Human approval queue")
        for item in pending:
            _print(f"{item['kind']} (priority {item['priority']}): {item['reason']}")
        if args.approve:
            for item in pending:
                outcome = await services.approvals.decide(item["id"], HitlDecisionRequest(action="approve"), "demo-reviewer")
                _print(f"  approved by demo-reviewer -> {outcome}")
        else:
            _print("  nothing was posted. Re-run with --approve to see what a human approval publishes.")

    posted = github.reviews.get((repo, 1), [])
    if posted:
        _rule("Posted to the pull request")
        _print(posted[-1]["body"])
        for comment in posted[-1]["comments"][: args.comments]:
            _print(f"\n--- inline comment on {comment['path']}:{comment.get('line')} ---")
            _print(comment["body"])

    _rule("Trace (event spine)")
    spans = [e for e in await store.get_trace(review_id) if e["event_type"] == "span.end"]
    for e in spans:
        _print(f"  {e['agent']:12} {e['name']:16} {e.get('latency_ms') or 0:>6} ms  {e.get('outcome')}")
    trace = await store.get_trace(review_id)
    cost = sum(float(e.get("cost_usd") or 0) for e in trace)
    _print(f"{len(trace)} events, {sum(1 for e in trace if e['event_type'] == 'llm.call')} model calls, ${cost:.4f}")
    await services.close()
    return 0


# --------------------------------------------------------------------------- eval
async def _eval(args: argparse.Namespace) -> int:
    from backend.evaluation.regression_gate import check_against_baseline, load_baseline, run_eval, update_baseline

    settings = get_settings()
    report = await run_eval(settings, use_configured_llm=not args.rules, judge=args.judge)
    if args.json:
        _print(json.dumps(report.as_dict(), indent=2))
    else:
        _rule(f"Golden set ({report.mode})")
        for s in report.scores:
            mark = "PASS" if s.passed else "FAIL"
            _print(f"{mark}  {s.case_id:32} matched {s.matched}/{s.expected}  decision={s.decision or '-'}")
            for miss in s.missed:
                _print(f"        missed: {miss}")
            for hit in s.forbidden_hits:
                _print(f"        false alarm: {hit}")
            if not s.decision_ok:
                _print("        gate decision outside the allowed set")
            for leak in s.leaks:
                _print(f"        SECRET LEAK: {leak}")
        sm = report.summary
        _print(f"\nrecall {sm.recall:.0%}   false alarms {sm.forbidden_hits}   gate accuracy {sm.decision_accuracy:.0%}   "
               f"leaks {sm.leaks}   cost ${report.cost_usd:.4f}")
        _print("recall by agent: " + ", ".join(f"{a} {r:.0%}" for a, r in sm.recall_by_agent.items()))
        if report.judge:
            _print(f"LLM judge (advisory): {report.judge['mean']} keep-rate {report.judge['keep_rate']}")
    if args.update_baseline:
        update_baseline(report)
        _print(f"baseline updated for mode '{report.mode}'")
        return 0
    problems = check_against_baseline(report, load_baseline())
    if problems:
        _rule("REGRESSION GATE: BLOCKED")
        for p in problems:
            _print(f"  - {p}")
        return 1
    if not args.json:
        _print("\nregression gate: green")
    return 0


# ------------------------------------------------------------------------ migrate
async def _migrate(_: argparse.Namespace) -> int:
    from backend.database.pool import create_pool
    from backend.database.schema import migrate

    settings = get_settings()
    if not settings.database_url:
        _print("TIGER_DATABASE_URL is not set")
        return 2
    pool = await create_pool(settings.database_url, 1, 2)
    try:
        async with pool.acquire() as conn:
            report = await migrate(conn, settings.embedding_dimensions)
    finally:
        await pool.close()
    _print(f"database flavor: {report.flavor}")
    _print(f"extensions: {report.extensions}")
    _print(f"applied: {report.applied or 'nothing new'}")
    _print(f"skipped (not applicable here): {report.skipped}")
    for name, reason in report.degraded.items():
        _print(f"DEGRADED {name}: {reason}")
    return 0


# -------------------------------------------------------------------- index-local
async def _index_local(args: argparse.Namespace) -> int:
    from backend.container import build_services

    services = await build_services(get_settings(), role="cli")
    try:
        report = await services.indexer.index_local(args.repo, Path(args.path))
        _print(json.dumps({k: v for k, v in report.__dict__.items()}, indent=2, default=str))
        _print(json.dumps(await services.store.index_stats(args.repo), indent=2, default=str))
    finally:
        await services.close()
    return 0


# ------------------------------------------------------------------- send-webhook
def sample_payload(repo: str = "acme/shop", number: int = 7, sha: str | None = None, action: str = "opened") -> dict[str, Any]:
    sha = sha or uuid.uuid4().hex + uuid.uuid4().hex[:8]
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number, "state": "open", "draft": False, "title": "Add user lookup",
            "body": "Adds a lookup helper.", "html_url": f"https://github.com/{repo}/pull/{number}",
            "user": {"login": "octocat"}, "head": {"sha": sha, "ref": "feature"},
            "base": {"sha": "0" * 40, "ref": "main"},
        },
        "repository": {"full_name": repo, "private": False, "default_branch": "main"},
        "sender": {"login": "octocat"},
    }  # fmt: skip


async def _send_webhook(args: argparse.Namespace) -> int:
    import httpx

    from backend.webhook_receiver.signature import SIGNATURE_HEADER, compute_signature

    settings = get_settings()
    if args.payload:
        body = Path(args.payload).read_bytes()
    else:
        body = json.dumps(sample_payload(args.repo, args.number, args.sha)).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": args.event,
        "X-GitHub-Delivery": args.delivery or str(uuid.uuid4()),
    }
    secret = args.secret or settings.github_webhook_secret
    if args.unsigned:
        pass
    elif args.bad_signature:
        headers[SIGNATURE_HEADER] = "sha256=" + "0" * 64
    else:
        if not secret:
            _print("no secret: pass --secret or set GITHUB_WEBHOOK_SECRET (or use --unsigned)")
            return 2
        headers[SIGNATURE_HEADER] = compute_signature(secret, body)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(args.url.rstrip("/") + "/webhooks/github", content=body, headers=headers)
    _print(f"HTTP {resp.status_code}  delivery={headers['X-GitHub-Delivery']}")
    _print(resp.text)
    return 0 if resp.status_code < 500 else 1


# ------------------------------------------------------------------------- doctor
async def _doctor(_: argparse.Namespace) -> int:
    settings: Settings = get_settings()
    ok = True

    def line(name: str, state: str, detail: str = "") -> None:
        _print(f"  {name:28} {state:8} {detail}")

    _rule(f"pr-review-agent {__version__}")
    line("llm provider", settings.resolved_llm_provider,
         "deterministic rule packs only" if settings.resolved_llm_provider == "rules" else
         ", ".join(f"{r}={settings.model_for(r)}" for r in ("security", "quality", "tests", "docs")))
    line("embeddings", settings.resolved_embedding_provider, f"{settings.embedding_model} / {settings.embedding_dimensions} dims"
         if settings.resolved_embedding_provider == "openai" else "local hashing embedder (no API key)")
    line("autonomy", settings.autonomy_level, f"threshold {settings.hitl_confidence_threshold}, noise floor {settings.noise_floor}")
    line("budget", "set", f"${settings.daily_budget_usd}/day, ${settings.max_cost_per_review_usd}/review")
    line("webhook secret", "ok" if settings.github_webhook_secret else "MISSING", "" if settings.github_webhook_secret else "webhooks will be refused")
    auth = "github app" if settings.github_app_id and settings.private_key_pem else ("token" if settings.github_token else "MISSING")
    line("github auth", auth, "" if auth != "MISSING" else "set GITHUB_APP_ID + key, or GITHUB_TOKEN")
    line("api tokens", "ok" if settings.token_principals else "none", "" if settings.token_principals else "dashboard API open in development, disabled in production")

    if settings.database_url:
        try:
            from backend.database.pool import create_pool
            from backend.database.schema import migrate

            pool = await create_pool(settings.database_url, 1, 2)
            async with pool.acquire() as conn:
                report = await migrate(conn, settings.embedding_dimensions)
                version = await conn.fetchval("SHOW server_version")
            await pool.close()
            line("database", "ok", f"Postgres {version}, flavor {report.flavor}")
            for name, reason in report.degraded.items():
                line("  degraded", name, reason)
        except Exception as exc:  # noqa: BLE001
            ok = False
            line("database", "FAILING", f"{type(exc).__name__}: {exc}"[:160])
    else:
        line("database", "memory", "TIGER_DATABASE_URL not set: nothing survives a restart")

    if settings.redis_url:
        try:
            from backend.memory.redis_client import create_redis

            redis = create_redis(settings.redis_url)
            await redis.ping()
            await redis.aclose()
            line("redis", "ok", "job queue + workflow checkpoints")
        except Exception as exc:  # noqa: BLE001
            ok = False
            line("redis", "FAILING", f"{type(exc).__name__}: {exc}"[:160])
    else:
        line("redis", "inline", "REDIS_URL not set: jobs run in the API process, checkpoints are not durable")
    return 0 if ok else 1


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr-review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="review a diff end to end, offline")
    demo.add_argument("--case", default="sql-injection-fstring", help="id of a golden case to review")
    demo.add_argument("--diff", help="path to a unified diff ('-' for stdin) instead of a golden case")
    demo.add_argument("--title", help="pull request title to use with --diff")
    demo.add_argument("--repo-dir", help="local checkout to index first, so the review has repository context")
    demo.add_argument("--approve", action="store_true", help="approve whatever lands in the human queue")
    demo.add_argument("--rules", action="store_true", help="force the deterministic rule packs even if an API key is set")
    demo.add_argument("--comments", type=int, default=3, help="how many inline comments to print")
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(fn=_demo)

    ev = sub.add_parser("eval", help="golden set + regression gate")
    ev.add_argument("--rules", action="store_true", help="evaluate the rule packs only (what CI runs)")
    ev.add_argument("--judge", action="store_true", help="also grade findings with the LLM judge (advisory)")
    ev.add_argument("--update-baseline", action="store_true")
    ev.add_argument("--json", action="store_true")
    ev.set_defaults(fn=_eval)

    sub.add_parser("migrate", help="create or upgrade the schema").set_defaults(fn=_migrate)

    idx = sub.add_parser("index-local", help="index a local checkout into the semantic memory")
    idx.add_argument("repo", help="name to index under, e.g. acme/shop")
    idx.add_argument("path", help="directory to index")
    idx.set_defaults(fn=_index_local)

    hook = sub.add_parser("send-webhook", help="send a GitHub-style webhook to a running server")
    hook.add_argument("--url", default="http://localhost:8000")
    hook.add_argument("--event", default="pull_request")
    hook.add_argument("--payload", help="JSON file to send (default: a sample pull_request.opened)")
    hook.add_argument("--repo", default="acme/shop")
    hook.add_argument("--number", type=int, default=7)
    hook.add_argument("--sha")
    hook.add_argument("--secret")
    hook.add_argument("--delivery", help="reuse a delivery id to exercise idempotency")
    hook.add_argument("--unsigned", action="store_true", help="send without a signature (must be rejected)")
    hook.add_argument("--bad-signature", action="store_true", help="send with a wrong signature (must be rejected)")
    hook.set_defaults(fn=_send_webhook)

    sp = sub.add_parser("sample-payload", help="print a sample pull_request webhook payload")
    sp.add_argument("--repo", default="acme/shop")
    sp.add_argument("--number", type=int, default=7)
    sp.set_defaults(fn=None)

    sub.add_parser("doctor", help="check configuration and connectivity").set_defaults(fn=_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sample-payload":
        _print(json.dumps(sample_payload(args.repo, args.number), indent=2))
        return 0
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
