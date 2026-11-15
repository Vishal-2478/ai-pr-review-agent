# Checkpoint

Read this first when you come back to the project, whether you are a person or an agent starting with no
memory of the last session. Trust what you can re-run over what is written here.

## State

All ten milestones in [DONE.md](DONE.md) are built. Six are fully shown by their demo command. Four (M2,
M5, M7, M10) are shown only against stand-ins: the parts that need Tiger Cloud itself, a real model key or
a real GitHub App are marked **not shown**, as is the container build.

Check it yourself in about a minute:

```bash
pip install -e ".[dev]"
make test      # 148 tests (12 need TEST_DATABASE_URL / TEST_REDIS_URL and are skipped without them)
make lint
make eval      # 17 golden cases, regression gate
make demo
```

CI runs the same on every push against TimescaleDB + pgvectorscale and against plain Postgres + pgvector,
plus the multi-process drill (`scripts/dev/e2e.py`) and the dashboard build.

## Next steps, in order

1. **Put a real model behind it.** Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, run
   `python -m backend.cli eval --judge`, read the misses and false alarms, tune the prompts in
   `backend/prompts/templates/` (add `security.v2.md` rather than editing v1), then
   `eval --update-baseline`.
2. **Connect a scratch repository** following `docs/DEPLOY.md`, in `AUTONOMY_LEVEL=shadow`.
3. **Build and deploy the containers.** `make up` locally first; the Dockerfiles have never been built.
4. **Grow the golden set** from real reviews: every finding a human rejects or a reviewer misses becomes
   a case in `backend/evaluation/golden_dataset.py`.

## Rules for changing this codebase

- A change that touches a prompt, a model, a rule or a threshold must pass `make eval`.
- New behaviour gets a test named for what it guarantees; if it is an invariant, add it to
  `context-graph.json` (a test checks that file against the code).
- Concrete implementations are chosen only in `backend/container.py`.
- Nothing outside `backend/orchestrator/` imports LangGraph; no specialist imports a vendor SDK.
- Update this file and `DONE.md` when a "not shown" becomes shown.
