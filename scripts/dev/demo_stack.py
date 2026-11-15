"""A populated system on your laptop, with no accounts and no API keys.

    python -m scripts.dev.demo_stack          # then, in another terminal:
    cd frontend && npm install && npm run dev  # open http://localhost:3000

Starts the fake GitHub + model API and the real backend, then opens a handful
of sample pull requests so the dashboard has reviews, an approval queue, traces
and costs to look at. With TIGER_DATABASE_URL and REDIS_URL set it also starts
a real worker and uses Postgres and Redis; without them everything runs inside
the API process and is forgotten when you stop it.

Stop with Ctrl-C.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

import httpx

from scripts.dev import e2e

SAMPLES = [
    ("clean-documented-change", 21),
    ("sql-injection-fstring", 22),
    ("hardcoded-secret", 23),
    ("swallowed-exception", 24),
    ("missing-tests", 25),
    ("prompt-injection-in-diff", 26),
    ("command-injection-shell", 27),
    ("missing-docstring", 28),
]


async def main() -> int:
    e2e.API, e2e.FAKE = "http://127.0.0.1:8000", "http://127.0.0.1:9100"
    durable = bool((os.environ.get("TIGER_DATABASE_URL") or os.environ.get("DATABASE_URL")) and os.environ.get("REDIS_URL"))
    log_dir = Path(e2e.ROOT / ".local" / "demo-logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    procs = e2e.Procs(log_dir)
    env = {
        "GITHUB_API_URL": e2e.FAKE, "OPENAI_BASE_URL": f"{e2e.FAKE}/llm", "API_TOKENS": "",
        "MODEL_SECURITY": "gpt-6.1-sol", "MODEL_QUALITY": "gpt-6.1-sol", "MODEL_TESTS": "gpt-6.1-terra",
        "MODEL_DOCS": "gpt-6-luna", "MODEL_FALLBACK": "gpt-6.1-terra", "LOG_LEVEL": "INFO",
    }  # fmt: skip
    if not durable:
        env.update(TIGER_DATABASE_URL="", DATABASE_URL="", REDIS_URL="")
    try:
        procs.start("fake", "uvicorn", "scripts.dev.fake_services:app", "--port", "9100", "--log-level", "warning", **env)
        procs.start("api", "uvicorn", "backend.main:app", "--port", "8000", "--log-level", "warning", **env)
        if durable:
            procs.start("worker", "arq", "backend.job_queue.worker.WorkerSettings", **env)
        async with httpx.AsyncClient(timeout=20.0) as client:
            await e2e._wait_http(client, f"{e2e.FAKE}/_admin/state")
            await e2e._wait_http(client, f"{e2e.API}/healthz")
            print(f"backend up at {e2e.API} ({'Postgres + Redis + worker' if durable else 'in-memory, single process'})")
            for case_id, number in SAMPLES:
                payload, _ = await e2e._open_pr(client, case_id, number)
                resp = await e2e._webhook(client, payload)
                print(f"  opened sample pull request #{number} ({case_id}): HTTP {resp.status_code}")
                await asyncio.sleep(0.4)
            print("\nDashboard: cd frontend && npm run dev, then open http://localhost:3000")
            print(f"API docs:  {e2e.API}/docs      webhook secret: {e2e.SECRET}")
            print("Ctrl-C to stop.")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.Event().wait()  # until Ctrl-C
    except KeyboardInterrupt:
        pass
    finally:
        procs.stop_all()
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
