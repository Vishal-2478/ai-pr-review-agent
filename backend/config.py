"""Runtime configuration.

Every knob the design calls a "decision" lives here so it is visible, versioned
and overridable from the environment. Nothing in this file imports another
backend module: ``core`` and ``config`` sit at the bottom of the dependency
graph (ADR-002).
"""

from __future__ import annotations

from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Default model per role for each provider. Any MODEL_* setting overrides its entry.
PROVIDER_MODELS: dict[str, dict[str, str]] = {
    "openai": {
        "security": "gpt-6.1-sol", "quality": "gpt-6.1-sol", "tests": "gpt-6.1-terra",
        "docs": "gpt-6-luna", "judge": "gpt-6.1-terra", "fallback": "gpt-6.1-terra",
    },
    "anthropic": {
        "security": "claude-opus-5-5", "quality": "claude-sonnet-5-5", "tests": "claude-sonnet-5-5",
        "docs": "claude-haiku-5-5", "judge": "claude-sonnet-5-5", "fallback": "claude-sonnet-5-5",
    },
}  # fmt: skip


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"  # development | test | production
    log_level: str = "INFO"

    # --- GitHub -----------------------------------------------------------
    github_webhook_secret: str = ""
    github_app_id: str = ""
    github_private_key: str = ""  # inline PEM (handy on Railway); "\n" escapes are expanded
    github_private_key_path: str = ""
    github_token: str = ""  # personal access token fallback when no GitHub App is configured
    github_api_url: str = "https://api.github.com"
    bot_login: str = ""  # login the reviewer posts as; its own comments are ignored
    allowed_repos: str = ""  # comma-separated globs ("acme/*"); empty = every repo that sends webhooks
    review_drafts: bool = False
    max_webhook_bytes: int = 5_000_000

    # --- Data layer -------------------------------------------------------
    database_url: str = Field(default="", validation_alias=AliasChoices("TIGER_DATABASE_URL", "DATABASE_URL"))
    redis_url: str = ""
    db_pool_min: int = 1
    db_pool_max: int = 8

    # --- LLM / embeddings -------------------------------------------------
    # auto: use the first provider that has a key (anthropic, then openai), else "rules".
    llm_provider: str = "auto"  # auto | openai | anthropic | rules
    openai_api_key: str = ""
    openai_base_url: str = ""  # any OpenAI-compatible endpoint (OpenRouter, a gateway, ...)
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    # Per-agent routing: the highest-stakes concern gets the strongest model,
    # the cheapest model handles docs. Empty = the provider default for that role.
    model_security: str = ""
    model_quality: str = ""
    model_tests: str = ""
    model_docs: str = ""
    model_judge: str = ""
    model_fallback: str = ""
    llm_max_output_tokens: int = 2500
    llm_temperature: float | None = None  # None = provider default (reasoning models reject it)
    embedding_provider: str = "auto"  # auto | openai | hash
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 256  # must match VECTOR(n) on code_chunks

    # --- Orchestration ----------------------------------------------------
    workflow_engine: str = "langgraph"  # langgraph | asyncio
    checkpoint_ttl_s: int = 604_800
    agent_timeout_s: float = 90.0
    llm_timeout_s: float = 60.0
    retrieval_timeout_s: float = 20.0
    github_timeout_s: float = 20.0

    # --- Diff and retrieval budgets ----------------------------------------
    max_diff_chars: int = 60_000
    max_files_per_review: int = 60
    retrieval_top_k: int = 8
    retrieval_token_budget: int = 6000

    # --- Selectivity and autonomy (the seed of the architecture) -----------
    # shadow: nothing is posted; human_approves_all: every review waits for a
    # person; exceptions_only: confident reviews post, the rest go to a person.
    autonomy_level: str = "exceptions_only"
    hitl_confidence_threshold: float = 0.6
    noise_floor: float = 0.3  # findings below this confidence are never surfaced
    max_inline_comments: int = 12
    post_clean_reviews: bool = False  # stay silent when there is nothing worth saying
    withhold_critical_security_from_public: bool = True

    # --- Economics ---------------------------------------------------------
    daily_budget_usd: float = 5.0
    max_cost_per_review_usd: float = 0.75
    budget_downgrade_ratio: float = 0.8  # past this share of the cap, route to cheaper models

    # --- Learning loop -----------------------------------------------------
    feedback_min_evidence: int = 5  # verdicts needed before feedback changes behaviour
    feedback_min_actors: int = 2  # ...from at least this many different people
    feedback_half_life_days: float = 30.0

    # --- Reliability -------------------------------------------------------
    retry_attempts: int = 3
    retry_base_delay_s: float = 0.5
    breaker_failure_threshold: int = 4
    breaker_recovery_s: float = 30.0
    idempotency_ttl_s: int = 259_200
    job_max_tries: int = 3

    # --- Dashboard / alerting ----------------------------------------------
    api_tokens: str = ""  # "token:role,token:role"; roles: viewer < reviewer < admin
    cors_origins: str = "http://localhost:3000"
    slack_webhook_url: str = ""
    escalation_rate_alert: float = 0.5
    hitl_queue_depth_alert: int = 25
    otel_exporter_otlp_endpoint: str = ""

    @field_validator(
        "hitl_confidence_threshold", "noise_floor", "budget_downgrade_ratio", "escalation_rate_alert"
    )
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("must be between 0 and 1")
        return v

    # ----------------------------------------------------------------------
    @property
    def resolved_llm_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key:
            return "openai"
        return "rules"

    @property
    def resolved_embedding_provider(self) -> str:
        if self.embedding_provider != "auto":
            return self.embedding_provider
        return "openai" if self.openai_api_key else "hash"

    @property
    def private_key_pem(self) -> str:
        if self.github_private_key:
            return self.github_private_key.replace("\\n", "\n")
        if self.github_private_key_path and Path(self.github_private_key_path).exists():
            return Path(self.github_private_key_path).read_text(encoding="utf-8")
        return ""

    @property
    def token_principals(self) -> dict[str, tuple[str, str]]:
        """token -> (role, name), parsed from API_TOKENS ("token:role[:name]", comma separated)."""
        out: dict[str, tuple[str, str]] = {}
        for item in filter(None, (p.strip() for p in self.api_tokens.split(","))):
            token, _, rest = item.partition(":")
            role, _, name = rest.partition(":")
            if token.strip():
                role = role.strip() or "viewer"
                out[token.strip()] = (role, name.strip() or role)
        return out

    @property
    def token_roles(self) -> dict[str, str]:
        return {token: role for token, (role, _) in self.token_principals.items()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in ("production", "prod")

    def repo_allowed(self, full_name: str) -> bool:
        patterns = [p.strip() for p in self.allowed_repos.split(",") if p.strip()]
        if not patterns:
            return True
        return any(fnmatch(full_name.lower(), p.lower()) for p in patterns)

    def model_for(self, role: str) -> str:
        """Model for a role: the explicit MODEL_* setting, else the provider default."""
        explicit = {
            "security": self.model_security,
            "quality": self.model_quality,
            "tests": self.model_tests,
            "docs": self.model_docs,
            "judge": self.model_judge,
            "fallback": self.model_fallback,
        }.get(role, "")
        if explicit:
            return explicit
        defaults = PROVIDER_MODELS.get(self.resolved_llm_provider, {})
        return defaults.get(role) or self.model_fallback or defaults.get("fallback", "rules")


@lru_cache
def get_settings() -> Settings:
    return Settings()
