"""In-memory GitHub used by the offline demo, the tests and fault-injection drills."""

from __future__ import annotations

from typing import Any

from backend.integrations.github_client import GitHubError
from backend.models.schemas import DiffFile


class FakeGitHubClient:
    def __init__(self) -> None:
        self.pulls: dict[tuple[str, int], dict[str, Any]] = {}
        self.files: dict[tuple[str, int], list[DiffFile]] = {}
        self.reviews: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.trees: dict[str, dict[str, str]] = {}  # repo -> path -> content
        self.failures: list[BaseException] = []  # raised, in order, by the next calls
        self.reject_inline_comments = False
        self.calls: list[str] = []
        self._next_id = 1000

    # ----------------------------------------------------------------- set-up
    def add_pull(
        self, repo: str, number: int, head_sha: str, files: list[DiffFile], *, title: str = "", body: str = "",
        author: str = "octocat", base_ref: str = "main",
    ) -> dict[str, Any]:  # fmt: skip
        pull = {
            "number": number, "title": title, "body": body, "draft": False, "state": "open",
            "html_url": f"https://github.com/{repo}/pull/{number}", "user": {"login": author},
            "head": {"sha": head_sha, "ref": "feature"}, "base": {"sha": "base000", "ref": base_ref},
        }  # fmt: skip
        self.pulls[(repo, number)] = pull
        self.files[(repo, number)] = files
        return pull

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self.failures:
            raise self.failures.pop(0)

    # -------------------------------------------------------------------- API
    async def get_pull(self, repo: str, number: int, installation_id: int | None = None) -> dict[str, Any]:
        self._maybe_fail("get_pull")
        if (repo, number) not in self.pulls:
            raise GitHubError(404, "Not Found")
        return self.pulls[(repo, number)]

    async def list_pull_files(
        self, repo: str, number: int, installation_id: int | None = None, max_files: int = 300
    ) -> list[DiffFile]:
        self._maybe_fail("list_pull_files")
        return list(self.files.get((repo, number), []))[:max_files]

    async def list_reviews(self, repo: str, number: int, installation_id: int | None = None) -> list[dict[str, Any]]:
        self._maybe_fail("list_reviews")
        return list(self.reviews.get((repo, number), []))

    async def create_review(
        self, repo: str, number: int, *, commit_id: str, body: str, comments: list[dict[str, Any]],
        installation_id: int | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        self._maybe_fail("create_review")
        if comments and self.reject_inline_comments:
            raise GitHubError(422, "Unprocessable Entity: line must be part of the diff")
        self._next_id += 1
        review = {"id": self._next_id, "commit_id": commit_id, "body": body, "comments": comments, "event": "COMMENT"}
        self.reviews.setdefault((repo, number), []).append(review)
        return review

    async def create_issue_comment(
        self, repo: str, number: int, body: str, installation_id: int | None = None
    ) -> dict[str, Any]:
        self._maybe_fail("create_issue_comment")
        self._next_id += 1
        comment = {"id": self._next_id, "body": body}
        self.comments.setdefault((repo, number), []).append(comment)
        return comment

    async def get_tree(self, repo: str, ref: str, installation_id: int | None = None) -> list[dict[str, Any]]:
        from backend.data.ingestion import git_blob_sha

        self._maybe_fail("get_tree")
        return [
            {"path": path, "type": "blob", "sha": git_blob_sha(content.encode()), "size": len(content)}
            for path, content in self.trees.get(repo, {}).items()
        ]

    async def get_blob_text(self, repo: str, sha: str, installation_id: int | None = None) -> str | None:
        from backend.data.ingestion import git_blob_sha

        self._maybe_fail("get_blob_text")
        for content in self.trees.get(repo, {}).values():
            if git_blob_sha(content.encode()) == sha:
                return content
        return None
