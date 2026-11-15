"""Composition root: the one place where concrete implementations are chosen.

Everything else receives a :class:`Services` and depends on interfaces, which
is what lets the same code run against Tiger Cloud + Redis + OpenAI in
production and against in-memory fakes in the demo and the tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.agents import AgentDeps, SpecialistAgent, build_specialists
from backend.agents.rules import run_rules
from backend.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError
from backend.core.workflow_engine import WorkflowEngine
from backend.data.ingestion import RepoIndexer
from backend.database.memory_store import InMemoryStore
from backend.economics.budget_guard import BudgetGuard
from backend.hitl.dispute import DisputeHandler
from backend.hitl.escalation import QueueMonitor
from backend.hitl.feedback import FeedbackCalibrator
from backend.hitl.queue import ApprovalQueue
from backend.integrations.publisher import ReviewPublisher
from backend.job_queue import jobs
from backend.job_queue.enqueue import ArqJobQueue, InlineJobQueue
from backend.memory.context_retriever import ContextRetriever
from backend.memory.embedder import HashEmbedder, OpenAIEmbedder
from backend.observability.alerting import Alerter
from backend.observability.events import EventEmitter
from backend.observability.tracing import setup_tracer
from backend.orchestrator.nodes import ReviewNodes
from backend.prompts.registry import PromptRegistry, default_registry
from backend.reliability.circuit_breaker import BreakerRegistry
from backend.reliability.idempotency import InMemoryIdempotencyStore, RedisIdempotencyStore
from backend.tools.llm_client import AnthropicClient, OpenAIChatClient
from backend.tools.model_router import ModelRouter
from backend.tools.registry import READ_DIFF, Tool, ToolRegistry

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    store: Any
    emitter: EventEmitter
    alerter: Alerter
    breakers: BreakerRegistry
    github: Any
    llm: Any | None
    embedder: Any
    retriever: ContextRetriever
    prompts: PromptRegistry
    budget: BudgetGuard
    router: ModelRouter
    tools: ToolRegistry
    specialists: dict[str, SpecialistAgent]
    calibrator: FeedbackCalibrator
    publisher: ReviewPublisher
    approvals: ApprovalQueue
    disputes: DisputeHandler
    monitor: QueueMonitor
    indexer: RepoIndexer
    idempotency: Any
    queue: Any
    engine: WorkflowEngine
    redis: Any | None = None
    pool: Any | None = None
    db_flavor: str = "memory"

    async def close(self) -> None:
        for client in (self.github, self.llm, self.alerter):
            if hasattr(client, "aclose"):
                await client.aclose()
        if self.pool is not None:
            await self.pool.close()
        if self.redis is not None:
            await self.redis.aclose()


def assemble(
    settings: Settings,
    *,
    store: Any,
    github: Any,
    llm: Any | None,
    embedder: Any | None = None,
    queue: Any | None = None,
    checkpointer: Any | None = None,
    idempotency: Any | None = None,
    redis: Any | None = None,
    pool: Any | None = None,
    alerter: Alerter | None = None,
    db_flavor: str = "memory",
    breakers: BreakerRegistry | None = None,
) -> Services:
    """Wire a Services graph from already-built infrastructure pieces."""
    breakers = breakers or BreakerRegistry(settings.breaker_failure_threshold, settings.breaker_recovery_s)
    emitter = EventEmitter(store, tracer=setup_tracer(settings.otel_exporter_otlp_endpoint))
    alerter = alerter or Alerter(settings.slack_webhook_url)
    embedder = embedder or HashEmbedder(settings.embedding_dimensions)
    prompts = default_registry()
    budget = BudgetGuard(store, settings.daily_budget_usd, settings.max_cost_per_review_usd, settings.budget_downgrade_ratio)
    router = ModelRouter(settings, breakers)

    tools = ToolRegistry(emitter)
    tools.register(
        Tool(
            name="static_signals", capability=READ_DIFF, fn=run_rules,
            description="Deterministic pre-checks over the lines a pull request adds.",
        )  # fmt: skip
    )
    deps = AgentDeps(settings, llm, prompts, emitter, budget, router, breakers, tools)

    if queue is None:
        queue = InlineJobQueue()
    publisher = ReviewPublisher(store, github, emitter, settings)

    async def publish(review_id: str, finding_ids: list[str], actor: str, publish_withheld: bool) -> Any:
        return await queue.enqueue("publish_review", review_id, finding_ids, actor, publish_withheld)

    approvals = ApprovalQueue(store, emitter, alerter, settings, publish, comment=github.create_issue_comment)
    services = Services(
        settings=settings, store=store, emitter=emitter, alerter=alerter, breakers=breakers, github=github,
        llm=llm, embedder=embedder, retriever=ContextRetriever(store, embedder, emitter), prompts=prompts,
        budget=budget, router=router, tools=tools, specialists=build_specialists(deps),
        calibrator=FeedbackCalibrator(
            store, settings.feedback_min_evidence, settings.feedback_min_actors, settings.feedback_half_life_days
        ),
        publisher=publisher, approvals=approvals, disputes=DisputeHandler(store, emitter, github),
        monitor=QueueMonitor(store, alerter, settings), indexer=RepoIndexer(store, embedder, emitter),
        idempotency=idempotency or InMemoryIdempotencyStore(), queue=queue,
        engine=None,  # type: ignore[arg-type]  # set just below: the engine needs the services
        redis=redis, pool=pool, db_flavor=db_flavor,
    )  # fmt: skip
    services.engine = _build_engine(settings, services, checkpointer)

    if isinstance(queue, InlineJobQueue):
        queue.register("review_pull_request", lambda job: jobs.review_pull_request(services, job))
        queue.register("publish_review", lambda *a: jobs.publish_review(services, *a))
        queue.register("index_repository", lambda *a: jobs.index_repository(services, *a))
        queue.register("handle_dispute", lambda payload: jobs.handle_dispute(services, payload))
    return services


def _build_engine(settings: Settings, services: Services, checkpointer: Any | None) -> WorkflowEngine:
    nodes = ReviewNodes(services)
    if settings.workflow_engine == "asyncio":
        from backend.orchestrator.asyncio_engine import AsyncioEngine

        return AsyncioEngine(nodes)
    if settings.workflow_engine != "langgraph":
        raise ConfigurationError(f"unknown WORKFLOW_ENGINE '{settings.workflow_engine}'")
    from backend.orchestrator.langgraph_engine import LangGraphEngine

    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()  # no Redis: checkpoints last as long as the process
    return LangGraphEngine(nodes, checkpointer)


def build_llm(settings: Settings) -> Any | None:
    provider = settings.resolved_llm_provider
    if provider == "rules":
        return None
    if provider == "openai":
        if not settings.openai_api_key:
            raise ConfigurationError("LLM_PROVIDER=openai needs OPENAI_API_KEY")
        return OpenAIChatClient(settings.openai_api_key, settings.openai_base_url, settings.llm_temperature)
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ConfigurationError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        return AnthropicClient(settings.anthropic_api_key, settings.anthropic_base_url, settings.llm_temperature)
    raise ConfigurationError(f"unknown LLM_PROVIDER '{provider}' (use auto, anthropic, openai or rules)")


async def build_services(settings: Settings | None = None, *, role: str = "api", arq_pool: Any | None = None) -> Services:
    """Production wiring from environment configuration."""
    from backend.integrations.github_client import HttpGitHubClient

    settings = settings or get_settings()
    pool = redis = checkpointer = None
    store: Any
    flavor = "memory"

    if settings.database_url:
        from backend.database.pool import create_pool
        from backend.database.schema import migrate
        from backend.database.store import PostgresStore

        pool = await create_pool(settings.database_url, settings.db_pool_min, settings.db_pool_max)
        async with pool.acquire() as conn:
            report = await migrate(conn, settings.embedding_dimensions)
        flavor = report.flavor
        log.info("database ready: flavor=%s applied=%s", flavor, report.applied)
        store = PostgresStore(pool)
    else:
        log.warning("no TIGER_DATABASE_URL: using the in-memory store; nothing survives a restart")
        store = InMemoryStore()

    queue: Any
    idempotency: Any
    if settings.redis_url:
        from backend.memory.redis_client import arq_settings, create_redis
        from backend.orchestrator.redis_checkpointer import PlainRedisSaver

        redis = create_redis(settings.redis_url)
        idempotency = RedisIdempotencyStore(redis)
        checkpointer = PlainRedisSaver(redis, ttl_s=settings.checkpoint_ttl_s)
        if arq_pool is None:
            from arq import create_pool as create_arq_pool

            arq_pool = await create_arq_pool(arq_settings(settings.redis_url))
        queue = ArqJobQueue(arq_pool)
    else:
        log.warning("no REDIS_URL: jobs run in-process and checkpoints are not durable")
        idempotency = InMemoryIdempotencyStore()
        queue = InlineJobQueue(background=(role == "api"))

    breakers = BreakerRegistry(settings.breaker_failure_threshold, settings.breaker_recovery_s)
    embedder: Any
    if settings.resolved_embedding_provider == "openai":
        if not settings.openai_api_key:
            raise ConfigurationError("EMBEDDING_PROVIDER=openai needs OPENAI_API_KEY")
        embedder = OpenAIEmbedder(
            settings.openai_api_key, settings.embedding_model, settings.embedding_dimensions,
            base_url=settings.openai_base_url, breaker=breakers.get("embeddings"),
            retry_attempts=settings.retry_attempts,
        )  # fmt: skip
    else:
        embedder = HashEmbedder(settings.embedding_dimensions)

    services = assemble(
        settings, store=store, github=HttpGitHubClient(settings, breakers.get("github")), llm=build_llm(settings),
        embedder=embedder, queue=queue, checkpointer=checkpointer, idempotency=idempotency, redis=redis,
        pool=pool, db_flavor=flavor, breakers=breakers,
    )  # fmt: skip
    if isinstance(embedder, OpenAIEmbedder):
        embedder.attach_emitter(services.emitter)  # embedding spend lands on the event spine too
    return services
