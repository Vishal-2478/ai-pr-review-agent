"""GitHub API boundary.

Authenticates as a GitHub App installation when an app is configured (short
lived, repository-scoped tokens), and falls back to a personal access token
for single-repository setups. Every call goes through the same retry policy
and circuit breaker as the model calls.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Protocol

import httpx

from backend.config import Settings
from backend.core.exceptions import ConfigurationError, PermanentError, RetryableError
from backend.models.schemas import DiffFile
from backend.reliability.circuit_breaker import CircuitBreaker
from backend.reliability.retry import is_retryable, retry_async

log = logging.getLogger(__name__)


class GitHubError(PermanentError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub HTTP {status}: {message}")
        self.status = status


class GitHubClient(Protocol):
    async def get_pull(self, repo: str, number: int, installation_id: int | None = None) -> dict[str, Any]: ...
    async def list_pull_files(self, repo: str, number: int, installation_id: int | None = None, max_files: int = 300) -> list[DiffFile]: ...
    async def list_reviews(self, repo: str, number: int, installation_id: int | None = None) -> list[dict[str, Any]]: ...
    async def create_review(self, repo: str, number: int, *, commit_id: str, body: str, comments: list[dict[str, Any]], installation_id: int | None = None) -> dict[str, Any]: ...
    async def create_issue_comment(self, repo: str, number: int, body: str, installation_id: int | None = None) -> dict[str, Any]: ...
    async def get_tree(self, repo: str, ref: str, installation_id: int | None = None) -> list[dict[str, Any]]: ...
    async def get_blob_text(self, repo: str, sha: str, installation_id: int | None = None) -> str | None: ...


class HttpGitHubClient:
    def __init__(self, settings: Settings, breaker: CircuitBreaker | None = None, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._breaker = breaker or CircuitBreaker("github")
        self._client = client or httpx.AsyncClient(base_url=settings.github_api_url, timeout=settings.github_timeout_s)
        self._tokens: dict[int, tuple[str, float]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------- auth
    def _app_jwt(self) -> str:
        import jwt

        pem = self._settings.private_key_pem
        if not (self._settings.github_app_id and pem):
            raise ConfigurationError("GitHub App credentials are not configured")
        now = int(time.time())
        # iat is backdated 60s to tolerate clock drift; GitHub caps exp at 10 minutes.
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._settings.github_app_id}, pem, algorithm="RS256")

    async def _token(self, installation_id: int | None) -> str:
        s = self._settings
        if installation_id and s.github_app_id and s.private_key_pem:
            cached = self._tokens.get(installation_id)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
            resp = await self._client.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._app_jwt()}", "Accept": "application/vnd.github+json"},
            )
            if resp.status_code >= 400:
                raise GitHubError(resp.status_code, f"could not mint installation token: {resp.text[:200]}")
            data = resp.json()
            self._tokens[installation_id] = (data["token"], time.time() + 50 * 60)
            return data["token"]
        return s.github_token

    # ---------------------------------------------------------------- request
    async def _request(self, method: str, path: str, installation_id: int | None, **kwargs: Any) -> httpx.Response:
        async def once() -> httpx.Response:
            token = await self._token(installation_id)
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                resp = await self._client.request(method, path, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise RetryableError(f"GitHub unreachable: {type(exc).__name__}") from exc
            rate_limited = resp.status_code == 429 or (
                resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"
            )
            if resp.status_code >= 500 or rate_limited:
                raise RetryableError(f"GitHub HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise GitHubError(resp.status_code, resp.text[:300])
            return resp

        return await retry_async(
            lambda: self._breaker.call(once, counts_as_failure=is_retryable),
            attempts=self._settings.retry_attempts,
            base_delay=self._settings.retry_base_delay_s,
        )

    # -------------------------------------------------------------------- API
    async def get_pull(self, repo: str, number: int, installation_id: int | None = None) -> dict[str, Any]:
        return (await self._request("GET", f"/repos/{repo}/pulls/{number}", installation_id)).json()

    async def list_pull_files(
        self, repo: str, number: int, installation_id: int | None = None, max_files: int = 300
    ) -> list[DiffFile]:
        files: list[DiffFile] = []
        page = 1
        while len(files) < max_files:
            resp = await self._request(
                "GET", f"/repos/{repo}/pulls/{number}/files", installation_id, params={"per_page": 100, "page": page}
            )
            batch = resp.json()
            for item in batch:
                files.append(
                    DiffFile(
                        path=item["filename"], status=item.get("status", "modified"),
                        additions=item.get("additions", 0), deletions=item.get("deletions", 0),
                        patch=item.get("patch") or "", previous_path=item.get("previous_filename"),
                    )  # fmt: skip
                )
            if len(batch) < 100:
                break
            page += 1
        return files[:max_files]

    async def list_reviews(self, repo: str, number: int, installation_id: int | None = None) -> list[dict[str, Any]]:
        resp = await self._request(
            "GET", f"/repos/{repo}/pulls/{number}/reviews", installation_id, params={"per_page": 100}
        )
        return resp.json()

    async def create_review(
        self, repo: str, number: int, *, commit_id: str, body: str, comments: list[dict[str, Any]],
        installation_id: int | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        # Always COMMENT: the agent advises. Approving or blocking a merge stays a human act.
        payload: dict[str, Any] = {"commit_id": commit_id, "body": body, "event": "COMMENT"}
        if comments:
            payload["comments"] = comments
        return (await self._request("POST", f"/repos/{repo}/pulls/{number}/reviews", installation_id, json=payload)).json()

    async def create_issue_comment(
        self, repo: str, number: int, body: str, installation_id: int | None = None
    ) -> dict[str, Any]:
        resp = await self._request("POST", f"/repos/{repo}/issues/{number}/comments", installation_id, json={"body": body})
        return resp.json()

    async def get_tree(self, repo: str, ref: str, installation_id: int | None = None) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"/repos/{repo}/git/trees/{ref}", installation_id, params={"recursive": "1"})
        data = resp.json()
        if data.get("truncated"):
            log.warning("tree for %s@%s was truncated by GitHub; index will be partial", repo, ref)
        return data.get("tree", [])

    async def get_blob_text(self, repo: str, sha: str, installation_id: int | None = None) -> str | None:
        data = (await self._request("GET", f"/repos/{repo}/git/blobs/{sha}", installation_id)).json()
        try:
            return base64.b64decode(data.get("content", "")).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return None  # binary file
