# Design

This document explains why the system has the shape it has. It follows the loop from the video the project
is built from: find the hardest problem, write down the choices it forces, repeat until the next step is
something you can build.

## The problem

"Send the diff to a model and post what it says" fails in three ways:

1. **No context.** The model has never seen the repository. It cannot know that `charge()` must go through
   the ledger, or that the team already rejected this suggestion twice.
2. **Hallucination.** It cites lines that do not exist and states guesses with full confidence.
3. **Noise.** It comments on everything. After a week of twenty comments per pull request, people stop
   reading, and the one comment that mattered is lost with the rest.

A senior engineer does the opposite. They know the codebase, they look at a change from several angles,
they say only what is worth saying, and when they are unsure they ask someone.

## The seed

> Optimise for surfacing the findings worth a senior engineer's attention and deferring the rest. Not for
> coverage, and not for volume.

Everything below is a consequence of that sentence. It is why confidence scoring, the gate and the human
queue are core parts of the system, not features added later.

## Five choices the seed forces

| Choice | Decision | Why |
| --- | --- | --- |
| Decomposition | Four specialists (security, quality, tests, docs) and an aggregator | Each concern gets its own prompt, model, evaluation and feedback history. A prompt that tries to do all four does none well, and one number cannot tell you which concern regressed. |
| Unit of work | The **Finding**: `{agent, severity, category, file, lines, evidence, rationale, suggestion, confidence}` | One typed contract flows from specialist to gate to queue to GitHub to feedback. A finding without quoted evidence is not a finding. |
| Trust boundary | The pull request (title, body, diff, comments) is untrusted input | It is fenced with an unguessable boundary, scanned for text addressed to the reviewer, and never able to change what the gate does. |
| Reversibility | Posting is the only irreversible act, so it is the only one that is gated | Reviews are advisory, posting is exactly-once, and any finding can be disputed and retracted. |
| Autonomy | `shadow` → `human_approves_all` → `exceptions_only` | Trust is earned per repository. Critical findings always go to a person, at every level. |

## Request lifecycle

1. **Ingress** (`webhook_receiver/`). Size cap, HMAC-SHA256 over the raw bytes, required headers, JSON,
   idempotency claim on the delivery id, enqueue, `202`. GitHub waits about ten seconds; this takes
   milliseconds because no review work happens here. If the queue is down the claim is released and the
   answer is `503`, so GitHub's own retry redelivers.
2. **Queue** (`job_queue/`). ARQ on Redis. The job id is derived from repository, pull request and head
   commit, so a manual redelivery under a new delivery id still maps to the same job, and the database
   enforces one review per commit with a unique constraint.
3. **Context** (`orchestrator/nodes.py`, `memory/`). Fetch the diff, mask secrets, scan for injection, and
   retrieve repository context: semantically similar code (vector search), code that names the same
   identifiers (full-text search), past findings on these files and how people judged them, and the
   team's conventions. Retrieval being down degrades the review; it does not fail it.
4. **Fan-out** (`agents/`). The four specialists run in parallel, each inside its own time budget. Each one
   runs its deterministic rule pack, checks the budget, routes to a model, calls it with retry and a
   circuit breaker, validates the output against the Finding contract, and grounds every finding against
   the diff. Every failure mode of a model (timeout, outage, unparseable output, exhausted budget) falls
   back to the rule pack and is recorded as reduced coverage.
5. **Fan-in** (`agents/aggregator.py`). Merge findings where two agents saw the same thing and count the
   agreement as evidence; keep different concerns on the same line apart; apply what human feedback has
   taught about this repository.
6. **Gate** (`hitl/gate.py`). A pure function, shown in full below.
7. **Publish or wait** (`integrations/publisher.py`, `hitl/queue.py`). Confident reviews are posted as one
   structured GitHub review with inline comments. The rest wait in Postgres for a person, who accepts or
   rejects finding by finding.

### The gate

```
finding below the noise floor                 -> suppressed (recorded, never shown)
low/info finding below the threshold          -> suppressed (not worth attention)
any CRITICAL finding                          -> escalate: alert a human, post nothing
medium+ finding the agent is unsure about     -> human approval queue
overall confidence below the threshold        -> human approval queue
untrusted input tried to steer the review     -> human approval queue
otherwise                                     -> post automatically
```

Overall confidence is the severity-weighted mean confidence of the surviving findings multiplied by
coverage (the share of specialists that completed normally). The gate is code, not a prompt: severity
routing must be replayable and testable, and a model must not be able to argue its way past it.

## Where confidence comes from

A model's self-reported confidence is an input, not the answer. The number on a finding is adjusted by:

- **grounding**: quoted evidence that is not in the diff, or a file that is not part of the change, cuts
  confidence and records why;
- **corroboration**: when two specialists independently flag the same place, the merged finding's
  confidence rises (the chance that at least one of them is right); when the model confirms a rule-pack
  signal, the finding keeps the higher of the two confidences and the higher severity;
- **deterministic floors**: a model cannot talk a rule-detected critical (a committed key) down;
- **calibration**: if several people have repeatedly rejected a category in this repository, its
  confidence is lowered. Feedback only ever lowers confidence, needs a minimum number of verdicts from
  more than one person, and decays with a half-life, so one person cannot train the reviewer into silence
  and an old mistake is not held against it forever.

## The data layer

One database, three kinds of data:

| Lane | Tables | Notes |
| --- | --- | --- |
| Memory | `code_chunks`, `repo_file_index`, `repo_conventions` | `VECTOR(256)` embedding with a DiskANN index and a generated `tsvector`. Indexing is incremental by blob hash: unchanged files are never re-embedded. |
| Truth | `pr_review_records`, `finding_records`, `hitl_reviews`, `hitl_feedback` | Unique on `(repo, pr_number, head_sha)`. Queue decisions are compare-and-set, so two reviewers cannot decide the same item. |
| Time | `agent_events` | Append-only: triggers reject `UPDATE`, `DELETE` and `TRUNCATE`. A hypertable with two continuous aggregates, `agent_health_1m` and `pr_cost_hourly`, which the budget guard and the dashboard read. |

Redis holds the job queue, idempotency keys and workflow checkpoints. It is scratch state: losing it loses
in-flight work, never a decision.

Tiger Cloud gives all three lanes in one Postgres. On plain Postgres with pgvector, the migration runner
applies an HNSW index and ordinary views with the same names instead. A Tiger-specific migration that fails
is logged, replaced by its fallback, and reported by `pr-review doctor`; `doctor --strict` turns that into
a failure, which is how CI proves the Tiger path really ran.

## Invariants

These hold on every path. Each is enforced by a test, so a change that breaks one fails the build.

| # | Invariant | Enforced by |
| --- | --- | --- |
| I1 | Nothing in a webhook is parsed or trusted before its signature verifies. | `test_signature_is_checked_before_the_body_is_parsed`, `test_missing_secret_fails_closed` |
| I2 | One review per commit, however many times the event arrives. | `test_same_commit_under_a_new_delivery_id_does_not_review_twice`, `test_one_review_per_commit_under_concurrency` |
| I3 | The ingress does no review work. | `test_ingress_does_no_review_work` |
| I4 | A slow or failing specialist never blocks the others or the join. | `test_a_hung_specialist_cannot_deadlock_the_join`, `test_model_failure_degrades_to_deterministic_rules` |
| I5 | A finding that cannot be grounded in the diff never reaches the pull request on its own authority. | `test_hallucinated_location_never_reaches_the_pull_request` |
| I6 | Routing is deterministic code; a critical finding always goes to a human. | `test_the_gate_is_deterministic_code`, `test_gate_decision_table`, `test_a_model_cannot_talk_a_deterministic_critical_down` |
| I7 | Nothing is posted while a human decision is pending, and a review is posted at most once. | `test_nothing_is_posted_until_a_human_decides`, `test_two_reviewers_cannot_decide_the_same_item` |
| I8 | Secrets found in a diff are masked before a model, a log, the database or GitHub sees them. | `test_secrets_in_the_diff_are_masked_before_the_model_sees_them`, golden-set leak check |
| I9 | Text in a pull request is data, never instruction. | `test_prompt_injection_in_the_diff_is_data_not_instruction` |
| I10 | The event log is append-only and every step writes to it. | `test_event_spine_is_append_only`, `test_every_step_is_on_the_event_spine` |
| I11 | Only the orchestrator knows the workflow framework; specialists never import a vendor SDK; `core` and `models` depend on nothing. | `tests/test_architecture.py` |
| I12 | Money is checked before it is spent. | `test_budget_guard_blocks_at_the_cap_and_downgrades_before_it`, `test_budget_exhaustion_falls_back_to_rules` |

## Failure modes

The video sorts failures on two axes: is it an engineering failure or a model failure, and does the system
notice it or not. The silent ones are the dangerous ones, so each has a mechanism that makes it visible.

| | **Loud** (the system notices) | **Silent** (looks like success) |
| --- | --- | --- |
| **Engineering** | Redis, Postgres, GitHub or the model API is down; a worker crashes mid-review. → retries with jitter, circuit breakers, checkpoint and resume, `503` to GitHub so it redelivers, degraded coverage reported on the review. | A duplicate delivery posts twice; a stale commit is reviewed; the index is out of date; the budget runs away. → idempotency at three layers, supersede on new commits, freshness tracking, budget guard before every call. |
| **Model** | Unparseable output, schema violations, timeouts. → contract validation, fallback to the rule pack, the failure recorded as an event. | A confident hallucination; a real vulnerability missed; a comment that obeys text planted in the diff; drift after a prompt or model change. → grounding, rule packs running beside the model, the injection guard, human feedback, and the golden-set regression gate. |

## Decisions

**ADR-001: LangGraph behind a `WorkflowEngine` interface.** LangGraph runs the fan-out and checkpoints
it, but only `orchestrator/graph.py`, `langgraph_engine.py` and the checkpointer import it. The nodes are
plain async functions. A second engine built on nothing but `asyncio` implements the same interface and
must reach the same verdict in tests, which keeps the seam honest. Moving to Temporal would mean writing
one more engine.

**ADR-002: a modular monolith.** One codebase and one image, run as two processes: the API and the
worker. Dependencies point inward (`core` and `models` import nothing else), and the composition root in
`container.py` is the only place that picks concrete implementations. Splitting into services later is
possible; starting with them would have meant a distributed system on day one.

**ADR-003: checkpoints on plain Redis commands.** The off-the-shelf Redis checkpointer needs the JSON and
search modules, which the small managed Redis plans do not have. `PlainRedisSaver` uses hashes, sets and
`EXPIRE` only, so it runs wherever ARQ does. Pending writes are stored per task, which is why a resumed
review re-runs only the specialist that was interrupted.

**ADR-004: budget guard before the call.** Cost is computed when a call returns and stored on its event
row. The guard reads today's spend from the rollup before each call; near the cap the router downgrades
models, at the cap the specialists use their rule packs. A review is never dropped for lack of budget.

**ADR-005: human wait state in Postgres, not in a paused workflow.** A person may take days. An approval
has to survive a Redis flush and a deploy, so the workflow ends at "queued for a human" and approving
starts a separate, idempotent publish job.

## Where this differs from the video

- **Rule packs.** Each specialist has a deterministic rule pack that runs beside the model. It gives
  agreement signals, a fallback when the model is unavailable, and a way to run the whole system and its
  regression gate with no API key.
- **Two providers.** Claude through the Messages API and OpenAI (or any OpenAI-compatible gateway).
- **Full-text search uses the `simple` configuration**, not `english`: stemming and stop-words hurt when
  the "words" are identifiers.
- **HITL is a queue in Postgres** rather than a workflow interrupt (ADR-005).
- **Severity comes from the specialist and is then bounded by code**: each agent has a maximum severity
  it may assign, and rule-detected criticals cannot be lowered by a model.
- **Not built:** fine-grained per-team policies, SSO for the dashboard, a tarball indexer for very large
  repositories, and auto-fix suggestions as commits.
