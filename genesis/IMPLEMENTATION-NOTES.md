# Implementation notes

Things a future session (human or agent) needs to know that the code alone does not say.

## How this was built

By Claude (Anthropic), working from the video transcript, the write-up and the Genesis kit's README, in
one sitting with no cloud accounts and no model API keys. That constraint shaped the build: everything had
to be provable offline, which is why there are in-memory implementations of every port, a fake GitHub, a
fake model server, and rule packs that can stand in for the models.

The Genesis kit's own CLI was **not** run against this repository, and nothing here records a human
approval: nobody but the builder has reviewed this code yet. This folder follows the ritual the video
shows (done-contract first, a demo command per milestone, notes and a context graph kept beside the code)
as plain files. To adopt the kit's tracked workflow, install it and run
`genesis init . --workflow new-product --objective "<your goal>"` as its README describes; it will ask
you, not an agent, to approve the spec.

## Decisions

The five architecture decisions are in [docs/DESIGN.md](../docs/DESIGN.md#decisions): LangGraph behind a
`WorkflowEngine` (ADR-001), modular monolith (ADR-002), checkpoints on plain Redis commands (ADR-003),
budget guard before the call (ADR-004), human wait state in Postgres (ADR-005).

Smaller choices worth knowing:

- **Ports with two implementations each.** `Store`, `JobQueue`, `IdempotencyStore`, `GitHubClient`,
  `LLMClient`, `Embedder` and `WorkflowEngine` all have a production and an in-memory implementation,
  chosen in `backend/container.py` and nowhere else. Tests and `make demo` use the in-memory side.
- **Migrations carry directives.** `-- requires:`, `-- provides:` and `-- optional` at the top of a SQL
  file let the runner apply Tiger-specific files where they work and fall back where they do not. A file
  runs as one implicit transaction, so a failed optional file leaves nothing behind.
- **Cost is stored, not recomputed.** Each model call's cost is computed from `pricing.json` when the call
  returns and written on its event row. Changing a price never rewrites history.
- **Feedback only lowers confidence.** See `backend/hitl/feedback.py`. Letting approvals raise confidence
  would let a streak of them walk a category past the human gate.
- **The Anthropic client uses plain HTTP**, not the SDK, so timeouts, retries and the breaker are this
  project's own and every attempt is on the event spine. The OpenAI client uses the SDK with its built-in
  retries switched off (`max_retries=0`) for the same reason.

## Lessons carried in from the video, and found while building

- From the video: Postgres rules do not work on hypertables, and `TRUNCATE` does not fire row triggers.
  The append-only guarantee therefore uses a row trigger for `UPDATE`/`DELETE` and a separate statement
  trigger for `TRUNCATE` (`035_events_append_only.sql`).
- The off-the-shelf Redis checkpointer needs Redis modules that small managed plans lack, hence
  `PlainRedisSaver`.
- A resumed LangGraph run only skips finished parallel branches if their pending writes were stored per
  task; the checkpointer's `aput_writes` exists for exactly this, and the kill-and-resume drill checks it.
- Reviewing this repository's own `Makefile` flagged `postgres:postgres@localhost` as a critical leaked
  credential. Default and templated passwords in connection strings are now ignored, with a golden case
  (`local-dev-connection-string`) so it stays fixed.

- **First real-model eval (OpenAI, 2026-11-15, run by the owner on Windows):** recall 67%, with
  security 100% and quality/tests/docs 0–50%, all of which the rule packs alone catch. The model invented its
  own category labels, and heuristic leads it did not repeat verbatim were dropped. Fix:
  `backend/agents/categories.py` (vocabulary + synonyms), prompt `base.v2` (numbered leads, required
  `dismissed_signals`), and `_reconcile` in `base_agent.py`. Leads are dropped only when the model gives a
  reason, and those reasons are on the event spine. A first attempt trimmed ignored leads' confidence by
  10%; a test showed that pushed low-severity leads under the gate threshold, so it was removed.

## Not verified

| Area | State |
| --- | --- |
| Real Anthropic calls | Never made; tested against a mocked transport. Model ids and prices were read from the providers' pricing pages on 2026-11-14. |
| Real OpenAI calls | Demo and golden set run by the owner. After the category fix: 17/17, recall 100%, 0 false alarms, 0 leaks, $0.22. One run only: variance between runs is not measured. |
| Real GitHub App | Never installed. The HTTP client is exercised against `scripts/dev/fake_services.py`, which imitates the endpoints used. |
| Docker images, `docker compose`, Railway | Written, never built or deployed: base images could not be pulled in the build environment. |
| Tiger Cloud | CI uses the `timescale/timescaledb-ha:pg17` image. The managed service was not used. |
| Load | No load or soak testing. `max_jobs = 4` per worker is a starting point, not a measurement. |

## Known gaps

- The dashboard does not refresh a decided item's findings until its next poll, so their status line lags
  a few seconds behind the confirmation.
- `ruff format` is not enforced; only `ruff check` runs in CI.
- The LLM judge is advisory and has no baseline of its own.
- Only Python is chunked on symbol boundaries.
