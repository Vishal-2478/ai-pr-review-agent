"""M4: orchestration. Engines are interchangeable, and a crash resumes from the checkpoint."""

from __future__ import annotations

import fakeredis
import pytest

from backend.core.workflow_engine import WorkflowEngine
from backend.job_queue import jobs
from backend.orchestrator.redis_checkpointer import PlainRedisSaver
from backend.tools.llm_client import ScriptedLLMClient
from tests.conftest import llm_finding, make_settings, open_pull


async def _outcome(services, job):
    result = await jobs.review_pull_request(services, job)
    review = await services.store.get_review(result["review_id"])
    findings = await services.store.get_findings(result["review_id"])
    return review, sorted((f["finding_id"], f["severity"], round(f["confidence"], 4), f["status"]) for f in findings)


async def test_both_engines_reach_the_same_verdict(make_services):
    """The WorkflowEngine interface is real: swap the orchestrator, get the same review."""
    outcomes = {}
    for engine in ("langgraph", "asyncio"):
        llm = ScriptedLLMClient({"security": {"findings": [llm_finding()]}})
        services = make_services(make_settings(workflow_engine=engine), llm=llm)
        assert isinstance(services.engine, WorkflowEngine) and services.engine.name == engine
        review, findings = await _outcome(services, open_pull(services))
        outcomes[engine] = (review["decision"], review["status"], review["overall_confidence"], findings)
    assert outcomes["langgraph"] == outcomes["asyncio"]


def _crash_once(services, method: str = "save_findings"):
    """Make the store fail the first time ``method`` is called, like a worker dying mid-review."""
    original = getattr(services.store, method)
    state = {"crashed": False}

    async def flaky(*args, **kwargs):
        if not state["crashed"]:
            state["crashed"] = True
            raise ConnectionError("worker lost its database connection")
        return await original(*args, **kwargs)

    setattr(services.store, method, flaky)
    return state


async def test_crash_after_fan_out_resumes_without_repeating_the_specialists(make_services):
    """The M4 demo: kill the worker mid-review, restart, and the review finishes from its checkpoint."""
    llm = ScriptedLLMClient({"security": {"findings": [llm_finding()]}})
    services = make_services(llm=llm)
    job = open_pull(services)
    state = _crash_once(services)

    with pytest.raises(ConnectionError):
        await jobs.review_pull_request(services, job)
    assert state["crashed"]
    assert len(llm.calls) == 4  # all four specialists had already finished and were checkpointed

    result = await jobs.review_pull_request(services, job)  # the queue's retry
    review = await services.store.get_review(result["review_id"])
    assert review["status"] == "posted"
    assert len(llm.calls) == 4, "resume must not pay for the specialists again"
    assert len(services.github.reviews[("acme/shop", 1)]) == 1


async def test_checkpoints_survive_a_process_restart_in_redis(make_services):
    """Same crash, but the 'restarted worker' is a brand-new engine sharing only Redis."""
    redis = fakeredis.FakeAsyncRedis()
    store_llm = ScriptedLLMClient({"security": {"findings": [llm_finding()]}})
    first = make_services(llm=store_llm, checkpointer=PlainRedisSaver(redis, ttl_s=600))
    job = open_pull(first)
    _crash_once(first)
    with pytest.raises(ConnectionError):
        await jobs.review_pull_request(first, job)
    assert await redis.keys("lg:*"), "the checkpoint must be in Redis, not in process memory"

    # A new process: new engine, new saver object, same Redis, same database, same GitHub.
    restarted_llm = ScriptedLLMClient({"security": {"findings": [llm_finding()]}})
    second = make_services(
        llm=restarted_llm, store=first.store, github=first.github, checkpointer=PlainRedisSaver(redis, ttl_s=600)
    )
    result = await jobs.review_pull_request(second, job)
    review = await second.store.get_review(result["review_id"])
    assert review["status"] == "posted"
    assert restarted_llm.calls == [], "the restarted worker reuses the checkpointed specialist results"
    assert await redis.ttl((await redis.keys("lg:cp:*"))[0]) > 0  # checkpoints are scratch state with a TTL


async def test_forget_removes_every_checkpoint_key(make_services):
    redis = fakeredis.FakeAsyncRedis()
    services = make_services(llm=ScriptedLLMClient(), checkpointer=PlainRedisSaver(redis, ttl_s=600))
    result = await jobs.review_pull_request(services, open_pull(services))
    assert await redis.keys("lg:*")
    await services.engine.forget(result["review_id"])
    assert await redis.keys("lg:*") == []


async def test_engine_state_is_inspectable(make_services):
    services = make_services(llm=ScriptedLLMClient())
    result = await jobs.review_pull_request(services, open_pull(services))
    state = await services.engine.get_state(result["review_id"])
    assert state.exists and state.finished and state.next_nodes == ()
    missing = await services.engine.get_state("no-such-review")
    assert not missing.exists
