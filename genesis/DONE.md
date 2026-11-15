# Done contract

What "finished" means for this project, written before the code and kept honest after it. Every milestone
has a demo command: a milestone is done when its command shows the behaviour, not when the code exists.

**Objective.** A pull request reviewer that surfaces the findings worth a senior engineer's attention and
defers the rest: four grounded specialists, a deterministic gate, a human approval queue, and a full trace
and cost record of every decision.

**Not in scope.** Approving, blocking or merging pull requests; auto-fix commits; SSO; per-team policy
engines; indexing very large monorepos.

## Acceptance criteria

The twelve invariants in [docs/DESIGN.md](../docs/DESIGN.md#invariants) hold on every path, each enforced by
a named test. The golden set passes the regression gate. CI is green on both database flavors.

## Milestones

Status is one of: **shown** (the demo command was run and behaved as described, here or in CI),
**not shown** (built, but the proof needs something that was unavailable during the build).

| # | Milestone | Demo command | What you should see | Status |
| --- | --- | --- | --- | --- |
| M1 | Webhook ingress | `python -m backend.cli send-webhook --unsigned`, then without `--unsigned`, then twice with `--delivery same` | `401`, then `202 queued`, then `200 duplicate`. Acknowledged in milliseconds. | shown: `tests/test_webhook_ingress.py`, e2e `[ingress]` |
| M2 | Tiger data layer | `python -m backend.cli migrate && python -m backend.cli doctor --strict` | Extensions enabled, hypertable, two continuous aggregates and the DiskANN index applied with no fallback. | shown in CI on `timescale/timescaledb-ha:pg17`; **not shown** on Tiger Cloud itself |
| M3 | Event spine | `make demo` (the *Trace* section), or `GET /api/reviews/{id}/trace` | A row for every span, model call and decision; `UPDATE`/`DELETE`/`TRUNCATE` on `agent_events` are refused. | shown: `test_every_step_is_on_the_event_spine`, `test_event_spine_is_append_only` |
| M4 | Orchestration | `make e2e` (the `[resume]` scenario) | The worker is stopped mid-review; a new worker finishes it and only the interrupted specialist runs again. | shown: e2e `[resume]`, `tests/test_engines.py`, `test_checkpoint_resume_on_real_redis` |
| M5 | Specialists and aggregator | `python -m backend.cli demo --case sql-injection-fstring --approve` | Findings from more than one specialist on one diff, each with quoted evidence and a confidence. | shown with rule packs and a fake model server; **not shown** with a real model |
| M6 | Retrieval | `python -m backend.cli demo --diff my.diff --repo-dir .` | "indexed N files into M chunks", and the review prompt carries the definitions the diff calls. | shown: `tests/test_memory.py`, `test_hybrid_retrieval_in_sql` |
| M7 | Gate, human queue, posting | `python -m backend.cli demo --case hardcoded-secret --approve` | The critical finding escalates, nothing is posted before approval, and the public review leaves the secret out. | shown: `tests/test_hitl.py`, e2e `[hitl]`, and by clicking through the dashboard; **not shown** against real GitHub |
| M8 | Reliability and economics | `make e2e` (the `[degrade]` scenario) | The security model is down; the review still completes with reduced coverage and the rule pack catches the injection. | shown: e2e `[degrade]`, `tests/test_reliability.py` |
| M9 | Dashboard | `make demo-stack`, then `make dashboard` | Approval queue, review page with trace waterfall, costs page. | shown: built in CI; pages and an approval exercised in a headless browser (screenshots in `docs/images/`) |
| M10 | Evaluation | `make eval` | 17 golden pull requests, recall and false alarms per agent, "regression gate: green". | shown in rule-pack mode; **not shown** with a real model |

## What is still open

1. Run `python -m backend.cli eval --judge` with a real API key and record a model baseline.
2. Install the GitHub App on a scratch repository and follow the smoke test in `docs/DEPLOY.md`.
3. Build the Docker images (`make up`) and deploy; neither was possible in the build environment.
4. Start the first real repository in `AUTONOMY_LEVEL=shadow`.
