"""Local stand-ins for GitHub and an OpenAI-compatible model API.

For end-to-end runs on a laptop or in CI without any external account:

    uvicorn scripts.dev.fake_services:app --port 9100

    GITHUB_API_URL=http://localhost:9100            # the review agent talks to this "GitHub"
    OPENAI_BASE_URL=http://localhost:9100/llm       # ...and to this "model"
    OPENAI_API_KEY=fake GITHUB_TOKEN=fake

The GitHub half behaves like the real API where it matters for this project:
a review comment on a line that is not part of the diff is refused with 422,
exactly the failure a careless publisher would hit in production.

The model half answers Chat Completions requests. It finds issues with the
project's own rule packs, so reviews look realistic, and each agent can be
slowed down or broken on demand (``POST /_admin/llm``) to rehearse timeouts,
fallbacks and killing a worker mid-review.

Never deploy this. It has no authentication.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from backend.agents import rules
from backend.integrations.diff import parse_patch, parse_unified_diff
from backend.memory.embedder import HashEmbedder
from backend.models.enums import AgentType
from backend.models.schemas import DiffFile

app = FastAPI(title="fake GitHub + fake model API (development only)")

_ids = itertools.count(1000)
PULLS: dict[tuple[str, int], dict[str, Any]] = {}
REVIEWS: dict[tuple[str, int], list[dict[str, Any]]] = {}
COMMENTS: dict[tuple[str, int], list[dict[str, Any]]] = {}
LLM: dict[str, Any] = {"delay": {}, "fail": {}, "calls": []}


# ------------------------------------------------------------------- admin
class PullIn(BaseModel):
    repo: str
    number: int
    sha: str
    diff: str
    title: str = "Test pull request"
    body: str = ""
    state: str = "open"
    tree: dict[str, str] = {}  # path -> file content, for the indexer


@app.post("/_admin/pulls")
async def register_pull(pull: PullIn) -> dict[str, Any]:
    files = parse_unified_diff(pull.diff)
    PULLS[(pull.repo, pull.number)] = {**pull.model_dump(), "files": files}
    return {"registered": f"{pull.repo}#{pull.number}", "files": len(files)}


class LlmIn(BaseModel):
    delay: dict[str, float] = {}  # agent -> seconds to wait before answering
    fail: dict[str, int] = {}  # agent -> HTTP status to answer with (0 = healthy)


@app.post("/_admin/llm")
async def configure_llm(cfg: LlmIn) -> dict[str, Any]:
    LLM["delay"], LLM["fail"] = cfg.delay, {k: v for k, v in cfg.fail.items() if v}
    return {"delay": LLM["delay"], "fail": LLM["fail"]}


@app.get("/_admin/state")
async def state() -> dict[str, Any]:
    return {
        "reviews": {f"{r}#{n}": v for (r, n), v in REVIEWS.items()},
        "comments": {f"{r}#{n}": v for (r, n), v in COMMENTS.items()},
        "llm_calls": LLM["calls"],
    }


@app.post("/_admin/reset")
async def reset() -> dict[str, str]:
    PULLS.clear(), REVIEWS.clear(), COMMENTS.clear()
    LLM.update(delay={}, fail={}, calls=[])
    return {"status": "reset"}


# ------------------------------------------------------------------ GitHub
def _pull(owner: str, repo: str, number: int) -> dict[str, Any]:
    pull = PULLS.get((f"{owner}/{repo}", number))
    if pull is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return pull


@app.get("/repos/{owner}/{repo}/pulls/{number}")
async def get_pull(owner: str, repo: str, number: int) -> dict[str, Any]:
    p = _pull(owner, repo, number)
    return {
        "number": number, "state": p["state"], "title": p["title"], "body": p["body"], "draft": False,
        "head": {"sha": p["sha"], "ref": "feature"}, "base": {"sha": "0" * 40, "ref": "main"},
        "user": {"login": "octocat"}, "html_url": f"https://github.example/{owner}/{repo}/pull/{number}",
    }  # fmt: skip


@app.get("/repos/{owner}/{repo}/pulls/{number}/files")
async def list_files(owner: str, repo: str, number: int, per_page: int = 30, page: int = 1) -> list[dict[str, Any]]:
    files: list[DiffFile] = _pull(owner, repo, number)["files"]
    window = files[(page - 1) * per_page : page * per_page]
    return [
        {"filename": f.path, "status": f.status, "additions": f.additions, "deletions": f.deletions,
         "patch": f.patch, "previous_filename": f.previous_path}
        for f in window
    ]  # fmt: skip


@app.get("/repos/{owner}/{repo}/pulls/{number}/reviews")
async def list_reviews(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    _pull(owner, repo, number)
    return REVIEWS.get((f"{owner}/{repo}", number), [])


@app.post("/repos/{owner}/{repo}/pulls/{number}/reviews")
async def create_review(owner: str, repo: str, number: int, request: Request) -> dict[str, Any]:
    p = _pull(owner, repo, number)
    payload = await request.json()
    if payload.get("commit_id") and payload["commit_id"] != p["sha"]:
        raise HTTPException(status_code=422, detail="commit_id is not the head of the pull request")
    by_path = {f.path: f for f in p["files"]}
    for comment in payload.get("comments") or []:
        file = by_path.get(comment.get("path"))
        if file is None:
            raise HTTPException(status_code=422, detail=f"path {comment.get('path')} is not part of the pull request")
        if comment.get("line") not in parse_patch(file.patch).commentable:
            raise HTTPException(status_code=422, detail="pull_request_review_thread.line must be part of the diff")
    review = {
        "id": next(_ids), "body": payload.get("body", ""), "state": "COMMENTED", "commit_id": payload.get("commit_id"),
        "comments": payload.get("comments") or [], "user": {"login": "pr-review-agent[bot]", "type": "Bot"},
        "html_url": f"https://github.example/{owner}/{repo}/pull/{number}#pullrequestreview",
        "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }  # fmt: skip
    REVIEWS.setdefault((f"{owner}/{repo}", number), []).append(review)
    return review


@app.post("/repos/{owner}/{repo}/issues/{number}/comments", status_code=201)
async def create_comment(owner: str, repo: str, number: int, request: Request) -> dict[str, Any]:
    _pull(owner, repo, number)
    comment = {"id": next(_ids), "body": (await request.json()).get("body", "")}
    COMMENTS.setdefault((f"{owner}/{repo}", number), []).append(comment)
    return comment


def _blob_sha(path: str) -> str:
    return base64.b16encode(path.encode()).decode().lower().ljust(40, "0")[:40]


@app.get("/repos/{owner}/{repo}/git/trees/{ref}")
async def get_tree(owner: str, repo: str, ref: str) -> dict[str, Any]:
    trees = [p["tree"] for (r, _), p in PULLS.items() if r == f"{owner}/{repo}"]
    tree = trees[-1] if trees else {}
    return {
        "sha": ref, "truncated": False,
        "tree": [{"path": path, "type": "blob", "sha": _blob_sha(path), "size": len(content)} for path, content in tree.items()],
    }  # fmt: skip


@app.get("/repos/{owner}/{repo}/git/blobs/{sha}")
async def get_blob(owner: str, repo: str, sha: str) -> dict[str, Any]:
    for (r, _), p in PULLS.items():
        if r != f"{owner}/{repo}":
            continue
        for path, content in p["tree"].items():
            if _blob_sha(path) == sha:
                return {"sha": sha, "encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
    raise HTTPException(status_code=404, detail="Not Found")


# ------------------------------------------------------------- model API
_CONCERN = re.compile(r"## Your concern: (\w+)")
_CONCERN_TO_AGENT = {
    "security": AgentType.SECURITY, "correctness": AgentType.QUALITY,
    "testing": AgentType.TESTS, "documentation": AgentType.DOCS,
}  # fmt: skip
_FILE_HEADER = re.compile(r"^### (?P<path>\S+) \((?P<status>\w+), \+(?P<add>\d+) -(?P<del>\d+)\)$")
_ADDED = re.compile(r"^\+ +\d+ \|(?: (.*))?$")
_CONTEXT = re.compile(r"^ +\d+ \|(?: (.*))?$")
_REMOVED = re.compile(r"^- +\|(?: (.*))?$")


def _agent_of(system_prompt: str) -> AgentType:
    m = _CONCERN.search(system_prompt)
    return _CONCERN_TO_AGENT.get(m.group(1).lower() if m else "", AgentType.QUALITY)


def _files_from_prompt(user_prompt: str) -> list[DiffFile]:
    """Turn the numbered diff the agent was shown back into patches."""
    files: list[DiffFile] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        if current and current["lines"]:
            files.append(DiffFile(
                path=current["path"], status=current["status"], additions=current["add"], deletions=current["del"],
                patch="\n".join(current["lines"]),
            ))  # fmt: skip

    for line in user_prompt.split("## Diff", 1)[-1].splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            flush()
            current = {"path": header["path"], "status": header["status"], "add": int(header["add"]),
                       "del": int(header["del"]), "lines": []}  # fmt: skip
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            current["lines"].append(line)
            continue
        for pattern, sign in ((_ADDED, "+"), (_REMOVED, "-"), (_CONTEXT, " ")):
            m = pattern.match(line)
            if m:
                current["lines"].append(sign + (m.group(1) or ""))
                break
    flush()
    return files


@app.post("/llm/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    body = await request.json()
    messages = body.get("messages") or []
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    agent = _agent_of(system)
    LLM["calls"].append({"agent": agent.value, "model": body.get("model"), "at": time.time()})

    if LLM["fail"].get(agent.value):
        raise HTTPException(status_code=int(LLM["fail"][agent.value]), detail="injected failure")
    if LLM["delay"].get(agent.value):
        await asyncio.sleep(float(LLM["delay"][agent.value]))

    findings = []
    try:
        for f in rules.run_rules(agent, _files_from_prompt(user)):
            findings.append({
                "severity": f.severity.value, "category": f.category, "summary": f.summary, "file_path": f.file_path,
                "line_start": f.line_start, "line_end": f.line_end, "evidence": f.evidence, "rationale": f.rationale,
                "suggestion": f.suggestion, "confidence": min(0.95, f.confidence + 0.2),
            })  # fmt: skip
    except Exception:  # noqa: BLE001 - a fake model may shrug
        findings = []
    content = json.dumps({"findings": findings})
    prompt_tokens = (len(system) + len(user)) // 4
    return {
        "id": f"chatcmpl-fake-{next(_ids)}", "object": "chat.completion", "created": int(time.time()),
        "model": body.get("model", "fake"),
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(content) // 4,
                  "total_tokens": prompt_tokens + len(content) // 4},
    }  # fmt: skip


@app.post("/llm/embeddings")
async def embeddings(request: Request) -> dict[str, Any]:
    body = await request.json()
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    vectors = await HashEmbedder(int(body.get("dimensions") or 256)).embed([str(i) for i in inputs])
    return {
        "object": "list", "model": body.get("model", "fake-embedding"),
        "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
        "usage": {"prompt_tokens": sum(len(str(i)) // 4 for i in inputs), "total_tokens": sum(len(str(i)) // 4 for i in inputs)},
    }  # fmt: skip
