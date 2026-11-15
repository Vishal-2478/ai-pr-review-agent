"""M8: retry, circuit breaker, timeouts, idempotency, budget and model routing."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from backend.core.exceptions import CircuitOpenError, NodeTimeoutError, PermanentError, RetryableError
from backend.economics.budget_guard import BudgetGuard, BudgetStatus
from backend.economics.pricing import cost_usd, is_priced
from backend.economics.routing_advisor import advise
from backend.reliability.circuit_breaker import BreakerRegistry, CircuitBreaker
from backend.reliability.idempotency import InMemoryIdempotencyStore, RedisIdempotencyStore
from backend.reliability.retry import backoff_delay, retry_async
from backend.reliability.timeout import with_timeout
from backend.tools.model_router import ModelRouter
from tests.conftest import make_settings


async def _no_sleep(_: float) -> None:
    return None


async def test_retry_recovers_from_transient_failures():
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RetryableError("503")
        return "ok"

    retried: list[int] = []
    assert await retry_async(flaky, attempts=3, sleep=_no_sleep, on_retry=lambda n, e, d: retried.append(n)) == "ok"
    assert attempts == 3 and retried == [1, 2]


async def test_retry_gives_up_and_never_retries_permanent_errors():
    calls = 0

    async def always() -> None:
        nonlocal calls
        calls += 1
        raise RetryableError("503")

    with pytest.raises(RetryableError):
        await retry_async(always, attempts=3, sleep=_no_sleep)
    assert calls == 3

    calls = 0

    async def bad_request() -> None:
        nonlocal calls
        calls += 1
        raise PermanentError("401 bad credentials")

    with pytest.raises(PermanentError):
        await retry_async(bad_request, attempts=5, sleep=_no_sleep)
    assert calls == 1  # asking again will not fix a 401


def test_backoff_is_bounded_and_jittered():
    delays = [backoff_delay(attempt, base=0.5, cap=8.0) for attempt in range(12) for _ in range(20)]
    assert all(0 <= d <= 8.0 for d in delays)
    assert len({round(d, 6) for d in delays}) > 50  # jitter: workers do not retry in lockstep


async def test_circuit_breaker_opens_fails_fast_and_recovers():
    now = [0.0]
    breaker = CircuitBreaker("llm", failure_threshold=3, recovery_s=30.0, clock=lambda: now[0])

    async def down() -> None:
        raise RetryableError("503")

    async def up() -> str:
        return "ok"

    for _ in range(3):
        with pytest.raises(RetryableError):
            await breaker.call(down)
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):  # fails fast: the dependency is not called
        await breaker.call(up)

    now[0] = 31.0
    assert breaker.state == "half_open"
    with pytest.raises(RetryableError):  # the probe fails: straight back to open
        await breaker.call(down)
    assert breaker.state == "open"

    now[0] = 62.0
    assert await breaker.call(up) == "ok"
    assert breaker.state == "closed"


async def test_half_open_admits_a_single_probe():
    now = [0.0]
    breaker = CircuitBreaker("gh", failure_threshold=1, recovery_s=10.0, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 11.0
    breaker.allow()  # the probe
    with pytest.raises(CircuitOpenError):
        breaker.allow()  # everyone else keeps failing fast until the probe reports


async def test_client_errors_do_not_trip_the_breaker():
    breaker = CircuitBreaker("gh", failure_threshold=2)

    async def not_found() -> None:
        raise PermanentError("404")

    for _ in range(5):
        with pytest.raises(PermanentError):
            await breaker.call(not_found, counts_as_failure=lambda e: not isinstance(e, PermanentError))
    assert breaker.state == "closed"


async def test_with_timeout_names_the_node():
    with pytest.raises(NodeTimeoutError) as info:
        await with_timeout(asyncio.sleep(5), 0.05, "docs")
    assert "docs" in str(info.value)


@pytest.mark.parametrize("factory", [InMemoryIdempotencyStore, lambda: RedisIdempotencyStore(fakeredis.FakeAsyncRedis())])
async def test_idempotency_claims_exactly_once(factory):
    store = factory()
    results = await asyncio.gather(*(store.claim("delivery:abc", 60) for _ in range(20)))
    assert results.count(True) == 1
    assert await store.claim("delivery:other", 60) is True
    await store.release("delivery:abc")
    assert await store.claim("delivery:abc", 60) is True


async def test_budget_guard_blocks_at_the_cap_and_downgrades_before_it(services):
    guard = BudgetGuard(services.store, daily_cap=1.0, per_review_cap=0.2, downgrade_ratio=0.8)
    assert (await guard.check()).allowed

    await services.emitter.emit("llm.call", agent="security", model="scripted", cost_usd=0.85, tokens_in=1, tokens_out=1, review_id=None)
    status = await guard.check()
    assert status.allowed and status.downgrade  # 85% of the cap: route to cheaper models

    await services.emitter.emit("llm.call", agent="security", model="scripted", cost_usd=0.2, tokens_in=1, tokens_out=1, review_id=None)
    status = await guard.check()
    assert not status.allowed and "daily cap" in status.reason


def test_model_routing_follows_risk_budget_and_breaker_state():
    settings = make_settings(model_security="strong", model_docs="cheap", model_fallback="backup")
    breakers = BreakerRegistry(failure_threshold=1, recovery_s=60)
    router = ModelRouter(settings, breakers)

    # The highest-stakes concern always starts on its configured model, with a fallback behind it.
    assert router.select("security", diff_chars=500).models == ("strong", "backup")
    assert router.select("docs", diff_chars=50_000).models == ("cheap", "backup")
    # Low-stakes agents on a small diff start on the cheaper fallback model.
    assert router.select("docs", diff_chars=500).primary == "backup"

    # Past the downgrade ratio of the daily budget, everyone starts on the fallback.
    pressured = BudgetStatus(allowed=True, spent_today=4.5, daily_cap=5.0, downgrade=True)
    assert router.select("security", diff_chars=500, budget=pressured).primary == "backup"

    breakers.get("llm:strong").record_failure()  # the strong model's circuit is open
    assert router.select("security", diff_chars=500).models == ("backup",)


def test_pricing_table_drives_cost():
    assert cost_usd("scripted", 1_000_000, 1_000_000) == pytest.approx(6.0)
    assert cost_usd("rules", 10_000, 10_000) == 0.0
    assert is_priced("claude-sonnet-5-5") and is_priced("gpt-6.1-sol")
    assert not is_priced("some-model-nobody-priced")
    assert cost_usd("some-model-nobody-priced", 1000, 1000) > 0  # unknown models are costed conservatively, never free


def test_routing_advisor_reads_the_rollups():
    health = [
        {"agent": "docs", "llm_calls": 40, "cost_usd": 1.0, "p95_latency_ms": 2000, "feedback_total": 20, "feedback_negative": 12, "errors": 0},
        {"agent": "security", "llm_calls": 40, "cost_usd": 4.0, "p95_latency_ms": 80_000, "feedback_total": 20, "feedback_negative": 1, "errors": 0},
        {"agent": "tests", "llm_calls": 2, "cost_usd": 0.1, "p95_latency_ms": 900, "feedback_total": 2, "feedback_negative": 2, "errors": 9},
    ]
    advice = {(a["agent"], a["kind"]) for a in advise(health, agent_timeout_s=90, min_evidence=5)}
    assert ("docs", "quality") in advice  # humans keep rejecting it: fix the prompt or the model
    assert ("security", "cost") in advice  # most of the spend, almost never wrong: try a cheaper model
    assert ("security", "latency") in advice  # p95 close to the node timeout
    assert ("tests", "reliability") in advice
    assert ("tests", "quality") not in advice  # two verdicts are not evidence
