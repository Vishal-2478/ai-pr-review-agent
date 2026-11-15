"""Shared fixtures: the whole system wired against in-memory fakes."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest

from backend.config import Settings
from backend.container import Services, assemble
from backend.database.memory_store import InMemoryStore
from backend.evaluation.golden_dataset import new_file
from backend.integrations.diff import parse_unified_diff
from backend.integrations.fake_github import FakeGitHubClient
from backend.models.schemas import DiffFile, PRContext
from backend.observability.alerting import Alerter

WEBHOOK_SECRET = "test-webhook-secret"

SQLI_DIFF = new_file(
    "app/users.py",
    '''
    """Users."""
    import sqlite3

    conn = sqlite3.connect("app.db")


    def get_user(user_id):
        """Return one user row."""
        query = f"SELECT * FROM users WHERE id = {user_id}"
        return conn.execute(query).fetchone()
    ''',
)

CLEAN_DIFF = new_file(
    "app/text.py",
    '''
    """Text helpers."""


    def shout(value):
        """Upper-case ``value``."""
        return value.upper()
    ''',
) + new_file(
    "tests/test_text.py",
    """
    from app.text import shout


    def test_shout():
        assert shout("a") == "A"
    """,
)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "github_webhook_secret": WEBHOOK_SECRET,
        "database_url": "",
        "redis_url": "",
        "llm_provider": "rules",
        "api_tokens": "",
        "retry_base_delay_s": 0.0,
        "model_security": "scripted",
        "model_quality": "scripted",
        "model_tests": "scripted",
        "model_docs": "scripted",
        "model_fallback": "scripted",
        "slack_webhook_url": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def make_services() -> Callable[..., Services]:
    def factory(settings: Settings | None = None, llm: Any | None = None, **kwargs: Any) -> Services:
        kwargs.setdefault("store", InMemoryStore())
        kwargs.setdefault("github", FakeGitHubClient())
        kwargs.setdefault("alerter", Alerter(""))
        return assemble(settings or make_settings(), llm=llm, **kwargs)

    return factory


@pytest.fixture
def services(make_services: Callable[..., Services]) -> Services:
    return make_services()


def files_from(diff: str) -> list[DiffFile]:
    return parse_unified_diff(diff)


def open_pull(
    services: Services, diff: str = SQLI_DIFF, *, repo: str = "acme/shop", number: int = 1,
    sha: str = "a" * 40, title: str = "Add user lookup", body: str = "",
) -> dict[str, Any]:  # fmt: skip
    """Register a pull request with the fake GitHub and return the job that reviews it."""
    services.github.add_pull(repo, number, sha, files_from(diff), title=title, body=body)
    pr = PRContext(
        repo=repo, pr_number=number, head_sha=sha, title=title, body=body, author="octocat",
        html_url=f"https://github.com/{repo}/pull/{number}", delivery_id="d-1",
    )  # fmt: skip
    return {"pr": pr.model_dump()}


def llm_finding(**over: Any) -> dict[str, Any]:
    base = {
        "severity": "high",
        "category": "sql-injection",
        "summary": "User input is interpolated into a SQL statement",
        "file_path": "app/users.py",
        "line_start": 9,
        "line_end": 9,
        "evidence": 'query = f"SELECT * FROM users WHERE id = {user_id}"',
        "rationale": "user_id reaches the query text unescaped, so a caller can change the statement.",
        "suggestion": "Use a parameterised query.",
        "confidence": 0.9,
    }
    base.update(over)
    return base


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
requires_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
requires_redis = pytest.mark.skipif(not TEST_REDIS_URL, reason="TEST_REDIS_URL not set")
