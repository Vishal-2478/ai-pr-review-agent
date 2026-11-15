"""Decide what, if anything, a verified GitHub event asks us to do.

The trigger is deliberately precise. GitHub sends every event the app is
subscribed to; only a handful of (event, action) pairs start work, and every
other delivery is acknowledged and dropped with a recorded reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.config import Settings
from backend.hitl.dispute import parse_disputes
from backend.integrations.github_models import REVIEWABLE_ACTIONS, pr_context_from_webhook


@dataclass
class WebhookAction:
    kind: str  # review | rereview | dispute | index | ping | ignored
    reason: str = ""
    job: str = ""
    args: tuple[Any, ...] = ()
    job_id: str | None = None
    extra: list[WebhookAction] = field(default_factory=list)


def _ignored(reason: str) -> WebhookAction:
    return WebhookAction(kind="ignored", reason=reason)


def parse_event(event: str, payload: dict[str, Any], delivery_id: str, settings: Settings) -> WebhookAction:
    if event == "ping":
        return WebhookAction(kind="ping", reason=payload.get("zen", "pong"))

    repo = (payload.get("repository") or {}).get("full_name", "")
    if repo and not settings.repo_allowed(repo):
        return _ignored(f"repository {repo} is not in ALLOWED_REPOS")
    installation_id = (payload.get("installation") or {}).get("id")
    sender = (payload.get("sender") or {}).get("login", "")

    if event == "pull_request":
        action = payload.get("action", "")
        if action not in REVIEWABLE_ACTIONS:
            return _ignored(f"pull_request action '{action}' does not trigger a review")
        pr = pr_context_from_webhook(payload, delivery_id)
        if pr.draft and not settings.review_drafts:
            return _ignored("draft pull request")
        if (payload["pull_request"].get("state") or "open") != "open":
            return _ignored("pull request is not open")
        return WebhookAction(
            kind="review", job="review_pull_request", args=({"pr": pr.model_dump()},),
            # One job per commit: a redelivery under a new delivery id still maps to the same job.
            job_id=f"review:{pr.key}",
        )  # fmt: skip

    if event in ("issue_comment", "pull_request_review_comment"):
        if payload.get("action") != "created":
            return _ignored("comment was not newly created")
        comment = payload.get("comment") or {}
        body = comment.get("body") or ""
        author = comment.get("user") or {}
        actor = author.get("login") or sender
        if author.get("type") == "Bot" or (settings.bot_login and actor.lower() == settings.bot_login.lower()):
            return _ignored("comment written by a bot")
        if event == "issue_comment":
            issue = payload.get("issue") or {}
            if "pull_request" not in issue:
                return _ignored("comment is on an issue, not a pull request")
            number = int(issue["number"])
        else:
            number = int((payload.get("pull_request") or {}).get("number", 0))
        disputes = parse_disputes(body)
        if not disputes or not number:
            return _ignored("comment contains no /dispute command")
        actions = [
            WebhookAction(
                kind="dispute", job="handle_dispute",
                args=({"repo": repo, "pr_number": number, "finding_id": fid, "reason": reason, "actor": actor,
                       "installation_id": installation_id},),
                job_id=f"dispute:{repo}:{number}:{fid}:{comment.get('id', delivery_id)}",
            )  # fmt: skip
            for fid, reason in disputes[:10]
        ]
        first = actions[0]
        first.extra = actions[1:]
        return first

    if event == "push":
        default_branch = (payload.get("repository") or {}).get("default_branch", "main")
        if payload.get("ref") != f"refs/heads/{default_branch}":
            return _ignored("push is not to the default branch")
        after = payload.get("after", "")
        if not after or set(after) == {"0"}:
            return _ignored("branch deleted")
        changed: set[str] = set()
        for commit in payload.get("commits") or []:
            for key in ("added", "modified", "removed"):
                changed.update(commit.get(key) or [])
        # Keep semantic memory fresh: re-embed only what this push touched.
        return WebhookAction(
            kind="index", job="index_repository",
            args=(repo, after, installation_id, sorted(changed)[:2000] or None), job_id=f"index:{repo}:{after}",
        )  # fmt: skip

    if event in ("installation", "installation_repositories"):
        action = payload.get("action", "")
        repos = payload.get("repositories") if event == "installation" else payload.get("repositories_added")
        if action not in ("created", "added") or not repos:
            return _ignored(f"{event} action '{action}' needs no work")
        allowed = [r["full_name"] for r in repos if settings.repo_allowed(r["full_name"])][:20]
        if not allowed:
            return _ignored("no installed repository is in ALLOWED_REPOS")
        actions = [
            WebhookAction(
                kind="index", job="index_repository", args=(name, "HEAD", installation_id, None),
                job_id=f"index:{name}:install:{delivery_id}",
            )  # fmt: skip
            for name in allowed
        ]
        first = actions[0]
        first.extra = actions[1:]
        return first

    return _ignored(f"event '{event}' is not handled")
