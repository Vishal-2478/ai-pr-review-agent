"""Integration: the real Postgres schema and a real Redis.

Skipped unless TEST_DATABASE_URL / TEST_REDIS_URL are set (CI sets both; locally
`make infra` starts them). These tests own the database they are pointed at:
they drop and recreate the schema.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.exceptions import ConfigurationError
from backend.database.pool import create_pool
from backend.database.schema import migrate, reset_schema
from backend.database.store import PostgresStore
from backend.evaluation.golden_dataset import get_case
from backend.job_queue import jobs
from backend.memory.redis_client import create_redis
from backend.models.schemas import HitlDecisionRequest, PRContext
from backend.orchestrator.redis_checkpointer import PlainRedisSaver
from backend.reliability.idempotency import RedisIdempotencyStore
from backend.tools.llm_client import ScriptedLLMClient
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, llm_finding, open_pull, requires_postgres, requires_redis
from tests.test_memory import CALLER_DIFF, PAYMENTS

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool():
    pool = await create_pool(TEST_DATABASE_URL, 1, 6)
    async with pool.acquire() as conn:
        await reset_schema(conn)
        await migrate(conn, 256)
    yield pool
    await pool.close()


@pytest.fixture
async def redis():
    client = create_redis(TEST_REDIS_URL)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@requires_postgres
async def test_migrations_are_idempotent_and_safe_to_run_concurrently(pool):
    async def run() -> list[str]:
        async with pool.acquire() as conn:
            return (await migrate(conn, 256)).applied

    results = await asyncio.gather(*(run() for _ in range(4)))  # API and workers booting together
    assert all(applied == [] for applied in results)
    async with pool.acquire() as conn:
        tables = {r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"pr_review_records", "finding_records", "hitl_reviews", "hitl_feedback", "agent_events", "code_chunks"} <= tables


@requires_postgres
async def test_fresh_database_survives_a_stampede_of_first_boots(pool):
    async with pool.acquire() as conn:
        await reset_schema(conn)

    async def boot() -> None:
        async with pool.acquire() as conn:
            await migrate(conn, 256)

    await asyncio.gather(*(boot() for _ in range(5)))
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM schema_migrations") >= 5


@requires_postgres
async def test_optional_migration_failure_falls_back_instead_of_failing_boot(pool, tmp_path: Path):
    """A Tiger-specific file that fails must degrade to its plain equivalent, loudly."""
    (tmp_path / "001_base.sql").write_text("CREATE TABLE IF NOT EXISTS fb_base (id int);")
    (tmp_path / "002_fancy.sql").write_text("-- provides: fancy_view\n-- optional\nCREATE VIEW fb_view AS SELECT no_such_function_anywhere();")
    (tmp_path / "003_plain.sql").write_text("-- requires: !fancy_view\nCREATE VIEW fb_view AS SELECT 1 AS n;")
    (tmp_path / "004_must.sql").write_text("CREATE TABLE fb_after (id int);")
    async with pool.acquire() as conn:
        await conn.execute("DROP VIEW IF EXISTS fb_view; DROP TABLE IF EXISTS fb_base, fb_after, fb_broken")
        report = await migrate(conn, 256, tmp_path)
        assert "002_fancy.sql" in report.degraded and "degraded" in report.flavor
        assert {"001_base.sql", "003_plain.sql", "004_must.sql"} <= set(report.applied)
        assert await conn.fetchval("SELECT n FROM fb_view") == 1

        (tmp_path / "005_broken.sql").write_text("CREATE TABLE fb_broken (id no_such_type);")
        with pytest.raises(ConfigurationError):  # a required migration still fails the boot
            await migrate(conn, 256, tmp_path)


@requires_postgres
async def test_embedding_dimension_mismatch_is_caught_at_startup(pool):
    async with pool.acquire() as conn:
        with pytest.raises(ConfigurationError) as info:
            await migrate(conn, 1536)
    assert "256" in str(info.value) and "1536" in str(info.value)


@requires_postgres
async def test_event_spine_is_append_only(pool, make_services):
    services = make_services(store=PostgresStore(pool))
    await services.emitter.emit("decision", agent="test", name="probe", outcome="ok")
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_events") == 1
        for statement in ("UPDATE agent_events SET agent = 'tampered'", "DELETE FROM agent_events", "TRUNCATE agent_events"):
            with pytest.raises(Exception) as info:  # noqa: B017, PT011
                await conn.execute(statement)
            assert "append-only" in str(info.value)
        assert await conn.fetchval("SELECT agent FROM agent_events") == "test"


@requires_postgres
async def test_one_review_per_commit_under_concurrency(pool):
    store = PostgresStore(pool)
    pr = PRContext(repo="acme/shop", pr_number=5, head_sha="c" * 40, title="race")
    results = await asyncio.gather(*(store.create_review(pr) for _ in range(12)))
    assert sum(1 for _, created in results if created) == 1
    assert len({review_id for review_id, _ in results}) == 1


@requires_postgres
async def test_full_review_and_approval_on_postgres(pool, make_services):
    llm = ScriptedLLMClient({
        "security": {"findings": [llm_finding()]},
        "quality": {"findings": [llm_finding(severity="medium", category="race-condition", confidence=0.45, summary="Possible race")]},
    })
    services = make_services(llm=llm, store=PostgresStore(pool), pool=pool)
    result = await jobs.review_pull_request(services, open_pull(services))
    store = services.store
    review = await store.get_review(result["review_id"])
    assert review["status"] == "pending_human" and review["cost_usd"] > 0

    item = (await store.list_hitl("pending"))[0]
    decisions = await asyncio.gather(
        services.approvals.decide(item["id"], HitlDecisionRequest(action="approve"), "alice"),
        services.approvals.decide(item["id"], HitlDecisionRequest(action="approve"), "bob"),
        return_exceptions=True,
    )
    assert sum(isinstance(d, dict) for d in decisions) == 1  # compare-and-set in SQL
    assert len(services.github.reviews[("acme/shop", 1)]) == 1
    assert (await store.get_review(result["review_id"]))["status"] == "posted"

    # Rollups (plain views here, continuous aggregates on Tiger) agree with the raw events.
    trace = await store.get_trace(result["review_id"])
    raw_cost = sum(float(e["cost_usd"] or 0) for e in trace)
    assert await store.cost_today() == pytest.approx(raw_cost, rel=1e-6)
    assert await store.review_cost(result["review_id"]) == pytest.approx(raw_cost, rel=1e-6)
    health = {a["agent"]: a for a in await store.agent_health(24)}
    assert health["security"]["llm_calls"] == 1 and health["security"]["cost_usd"] > 0
    assert (await store.top_reviews_by_cost())[0]["review_id"] == result["review_id"]
    assert (await store.hitl_stats())["pending"] == 0


@requires_postgres
async def test_new_commit_expires_stale_approvals_but_keeps_escalations(pool, make_services):
    unsure = llm_finding(severity="medium", category="race-condition", confidence=0.45)
    services = make_services(llm=ScriptedLLMClient({"quality": {"findings": [unsure]}}), store=PostgresStore(pool))
    old = await jobs.review_pull_request(services, open_pull(services, sha="1" * 40))
    secret = await jobs.review_pull_request(services, open_pull(services, get_case("hardcoded-secret").diff, number=2, sha="5" * 40))
    await jobs.review_pull_request(services, open_pull(services, sha="2" * 40))
    await jobs.review_pull_request(services, open_pull(services, get_case("clean-documented-change").diff, number=2, sha="6" * 40))

    assert (await services.store.get_review(old["review_id"]))["status"] == "superseded"
    pending = await services.store.list_hitl("pending")
    assert all(i["review_id"] != old["review_id"] for i in pending)
    # The leaked credential is still leaked after the next push: its escalation stays open.
    assert any(i["review_id"] == secret["review_id"] and i["kind"] == "escalation" for i in pending)


@requires_postgres
async def test_hybrid_retrieval_in_sql(pool, make_services, tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "payments.py").write_text(PAYMENTS)
    for i in range(12):
        (tmp_path / "app" / f"widget_{i}.py").write_text(f'"""Widget {i}."""\n\n\ndef render_widget_{i}(html):\n    return "<div>" + html + "</div>"\n')
    llm = ScriptedLLMClient()
    services = make_services(llm=llm, store=PostgresStore(pool))
    report = await services.indexer.index_local("acme/shop", tmp_path)
    assert report.indexed == 13

    store = services.store
    vector = (await services.embedder.embed(["charge_customer(customer_id, amount_cents)"]))[0]
    by_vector = await store.search_vector("acme/shop", vector, 3)
    by_text = await store.search_text("acme/shop", [["charge", "customer"]], 3)
    assert by_vector[0].path == "app/payments.py" and by_text[0].path == "app/payments.py"
    assert await store.search_vector("someone/else", vector, 3) == []  # tenancy: never another repository's code

    await jobs.review_pull_request(services, open_pull(services, CALLER_DIFF))
    prompt = llm.calls[0]["user"]
    assert "def charge_customer" in prompt and "amount must be positive" in prompt
    assert prompt.count("def render_widget_") < 12  # top-k, not the whole repository

    again = await services.indexer.index_local("acme/shop", tmp_path)
    assert again.indexed == 0 and again.unchanged == 13


@requires_redis
async def test_idempotency_claims_are_atomic_in_redis(redis):
    store = RedisIdempotencyStore(redis)
    results = await asyncio.gather(*(store.claim("delivery:xyz", 60) for _ in range(50)))
    assert results.count(True) == 1
    assert 0 < await redis.ttl("idem:delivery:xyz") <= 60


@requires_redis
async def test_checkpoint_resume_on_real_redis(redis, make_services):
    llm = ScriptedLLMClient({"security": {"findings": [llm_finding()]}})
    first = make_services(llm=llm, checkpointer=PlainRedisSaver(redis, ttl_s=120))
    job = open_pull(first)
    original = first.store.save_findings
    calls = {"n": 0}

    async def crash_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("worker died")
        return await original(*args, **kwargs)

    first.store.save_findings = crash_once  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await jobs.review_pull_request(first, job)

    fresh_llm = ScriptedLLMClient()
    second = make_services(llm=fresh_llm, store=first.store, github=first.github, checkpointer=PlainRedisSaver(redis, ttl_s=120))
    result = await jobs.review_pull_request(second, job)
    assert (await second.store.get_review(result["review_id"]))["status"] == "posted"
    assert fresh_llm.calls == []
    keys = await redis.keys("lg:*")
    assert keys and all([await redis.ttl(k) > 0 for k in keys])


@requires_redis
@requires_postgres
async def test_job_travels_through_arq_to_a_worker(pool, redis, make_services):
    """Enqueue on one side of Redis, execute on the other, exactly once per job id."""
    from arq import create_pool as arq_create_pool
    from arq.worker import Worker, func

    from backend.job_queue.enqueue import ArqJobQueue
    from backend.memory.redis_client import arq_settings

    arq_pool = await arq_create_pool(arq_settings(TEST_REDIS_URL))
    services = make_services(llm=ScriptedLLMClient({"security": {"findings": [llm_finding()]}}), store=PostgresStore(pool), queue=ArqJobQueue(arq_pool))
    job = open_pull(services)

    assert await services.queue.enqueue("review_pull_request", job, job_id="review:acme/shop:1:" + "a" * 40) is True
    assert await services.queue.enqueue("review_pull_request", job, job_id="review:acme/shop:1:" + "a" * 40) is False
    assert (await services.queue.stats())["queued"] == 1

    async def review(ctx, payload):
        return await jobs.review_pull_request(services, payload)

    async def index(ctx, repo, ref="HEAD", installation_id=None, only_paths=None):
        return await jobs.index_repository(services, repo, ref, installation_id, only_paths)

    worker = Worker(
        functions=[func(review, name="review_pull_request"), func(index, name="index_repository")],
        redis_pool=arq_pool, burst=True, poll_delay=0.05, handle_signals=False,
    )  # fmt: skip
    await worker.main()
    await worker.close()
    # The review, plus the background indexing of a repository seen for the first time.
    assert worker.jobs_complete == 2 and worker.jobs_failed == 0
    assert len(services.github.reviews[("acme/shop", 1)]) == 1
