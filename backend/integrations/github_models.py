"""Typed views over the GitHub webhook payloads this system reacts to."""

from __future__ import annotations

from typing import Any

from backend.models.schemas import PRContext

REVIEWABLE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


def pr_context_from_webhook(payload: dict[str, Any], delivery_id: str = "") -> PRContext:
    pr = payload["pull_request"]
    return PRContext(
        repo=payload["repository"]["full_name"],
        pr_number=int(pr["number"]),
        head_sha=pr["head"]["sha"],
        base_sha=(pr.get("base") or {}).get("sha", ""),
        base_ref=(pr.get("base") or {}).get("ref", ""),
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        author=(pr.get("user") or {}).get("login", ""),
        html_url=pr.get("html_url") or "",
        installation_id=(payload.get("installation") or {}).get("id"),
        delivery_id=delivery_id,
        draft=bool(pr.get("draft")),
    )


def pr_context_from_api(repo: str, pr: dict[str, Any], installation_id: int | None, delivery_id: str = "") -> PRContext:
    return pr_context_from_webhook(
        {"repository": {"full_name": repo}, "pull_request": pr, "installation": {"id": installation_id}},
        delivery_id,
    )
