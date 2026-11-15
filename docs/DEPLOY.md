# Going live

This takes the system from `make demo` to reviewing a real repository. Nothing here was exercised during
the build (no cloud accounts or API keys were available), so treat it as a checklist to follow on a scratch
repository first, and expect to adjust details.

You need four things: a database, a Redis, a GitHub App, and somewhere to run two processes.

## 1. Database: Tiger Cloud

1. Create a service at [tigerdata.com](https://www.tigerdata.com) and copy its connection string.
2. Set `TIGER_DATABASE_URL` to it. Keep `sslmode=require` in the URL.
3. Run `python -m backend.cli migrate`, then `python -m backend.cli doctor --strict`.

`migrate` enables `vector`, `timescaledb` and `vectorscale` if the service offers them and applies the
schema. `doctor --strict` exits non-zero if the hypertable, the continuous aggregates or the DiskANN index
had to fall back to their plain-Postgres equivalents. The API and worker also run migrations on start, so
step 3 is a check, not a requirement.

Any Postgres 15+ with pgvector works as well; you get an HNSW index and ordinary views in place of
DiskANN and continuous aggregates.

## 2. Redis

Any Redis 6+ without modules: Upstash, Railway's Redis, or your own. Set `REDIS_URL` (use `rediss://` for
TLS). It holds the job queue, idempotency keys and workflow checkpoints.

Without `REDIS_URL` the API runs jobs in its own process. That is fine for a trial and wrong for
production: there is no worker to scale and a restart loses in-flight reviews.

## 3. GitHub App

Create one under **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Setting | Value |
| --- | --- |
| Webhook URL | `https://<your-api-host>/webhooks/github` |
| Webhook secret | a long random string; the same value goes in `GITHUB_WEBHOOK_SECRET` |
| Repository permissions | **Pull requests**: read and write · **Contents**: read · **Issues**: read · **Metadata**: read |
| Subscribe to events | Pull request · Issue comment · Pull request review comment · Push |

Then generate a private key, install the App on one repository, and set:

```
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY_PATH=/path/to/key.pem      # or GITHUB_PRIVATE_KEY with \n for newlines
BOT_LOGIN=your-app-name[bot]
ALLOWED_REPOS=your-org/your-repo
```

What each event is for: pull requests trigger reviews, comments carry `/dispute F-xxxx reason`, and pushes
to the default branch refresh the index. `ALLOWED_REPOS` is a second lock: events from anywhere else are
acknowledged and ignored.

A fine-grained personal access token in `GITHUB_TOKEN` works instead of an App for a single repository
(same permissions). Reviews then appear under your own name.

## 4. Models

Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. With both, Anthropic is used for reviews and OpenAI for
embeddings; with only an Anthropic key, embeddings use the local hashing embedder (retrieval then leans on
the full-text half). Before trusting model reviews:

```bash
python -m backend.cli eval --judge            # golden set with your models; prints recall and cost
python -m backend.cli eval --update-baseline  # once you have read the report and accept it as the bar
```

The baseline in the repository covers only the rule-pack mode. Until you record one for your model
configuration, the gate holds model runs to the rule-pack baseline as a floor.

## 5. Run the two processes

One image, two commands:

```
api:     uvicorn backend.main:app --host 0.0.0.0 --port $PORT
worker:  arq backend.job_queue.worker.WorkerSettings
```

**Railway.** Create a project from this repository. `railway.json` builds the `Dockerfile` and
health-checks `/healthz`; that service is the API. Add a second service from the same repository with the
start command `arq backend.job_queue.worker.WorkerSettings` and no public domain. Give both the same
variables. For the dashboard, add a third service with root directory `frontend` and
`NEXT_PUBLIC_API_URL=https://<your-api-host>` set at build time.

**Anywhere else.** `docker compose --profile app up -d --build` runs the same three services next to a
local database and Redis; point `TIGER_DATABASE_URL` and `REDIS_URL` at managed ones for production.

Production settings that matter:

```
ENVIRONMENT=production                      # the dashboard API refuses to run without tokens
API_TOKENS=<token>:admin:you,<token>:reviewer:teammate
CORS_ORIGINS=https://<your-dashboard-host>
AUTONOMY_LEVEL=shadow                       # see step 7
SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # escalations and capacity alerts
```

## 6. Smoke test

```bash
curl https://<api>/healthz                         # {"status":"ok",...}
curl https://<api>/readyz                          # database, redis and webhook secret all "ok"
python -m backend.cli send-webhook --url https://<api> --unsigned     # 401
python -m backend.cli send-webhook --url https://<api> --secret ...   # 202 (ignored unless the repo is allowed)
```

Then open a pull request on the scratch repository. In GitHub, **App settings → Advanced → Recent
deliveries** should show a `202` within a second, and the review should appear in the dashboard with its
trace.

## 7. Roll out in stages

1. **`shadow`** for the first week or so. Every pull request is reviewed and recorded; nothing is posted.
   Read the findings in the dashboard and mark them accepted or rejected: that is what calibrates the
   reviewer for this repository.
2. **`human_approves_all`** once the findings are mostly ones you would have made. Everything waits for a
   click.
3. **`exceptions_only`** once approvals are routine. Confident reviews post on their own; uncertain ones
   and anything critical still wait.

Move back a stage whenever the escalation rate climbs. The dashboard and the Slack alert both report it.

## Operating it

| Signal | Where | What to do |
| --- | --- | --- |
| Escalation rate above `ESCALATION_RATE_ALERT`, or queue deeper than `HITL_QUEUE_DEPTH_ALERT` | Slack, the queue page | The agent is unsure too often. Check recent prompt or model changes, re-run `eval`, add reviewers, or step back an autonomy level. |
| Spend near `DAILY_BUDGET_USD` | Costs page | Past 80% the router moves to cheaper models; at the cap, specialists use rule packs. Raise the cap or change `MODEL_*`. |
| Coverage below 100% on reviews | Review page, trace | A specialist timed out or its model failed. The trace shows which call and why. |
| Circuit breaker open | `GET /api/system/status` | A dependency is failing. It closes by itself after a successful probe. |
| A review is wrong | The pull request or the dashboard | The author replies `/dispute F-xxxx reason`; a maintainer upholds or retracts it. Add the case to `backend/evaluation/golden_dataset.py`. |

Rotating the webhook secret: set the new value in GitHub and in `GITHUB_WEBHOOK_SECRET`, then restart the
API. Deliveries signed with the old secret are refused, and GitHub's redelivery button resends them.

Changing the embedding model or its dimensions means re-indexing: startup refuses to run when the
configured dimension does not match the `code_chunks` column.
