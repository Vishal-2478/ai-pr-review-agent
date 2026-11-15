"""M1: the ingress contract. Verify, deduplicate, enqueue, acknowledge."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.cli import sample_payload
from backend.main import create_app
from backend.webhook_receiver.signature import SIGNATURE_HEADER, compute_signature, verify_signature
from tests.conftest import SQLI_DIFF, WEBHOOK_SECRET, files_from, make_settings


def _headers(body: bytes, *, event: str = "pull_request", delivery: str = "d-1", secret: str | None = WEBHOOK_SECRET) -> dict[str, str]:
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}
    if secret is not None:
        headers[SIGNATURE_HEADER] = compute_signature(secret, body)
    return headers


@pytest.fixture
def client(make_services):
    services = make_services()
    with TestClient(create_app(services)) as c:
        c.services = services  # type: ignore[attr-defined]
        yield c


def _pr_body(sha: str = "b" * 40, **kwargs: Any) -> bytes:
    return json.dumps(sample_payload("acme/shop", 7, sha, **kwargs)).encode()


def test_unsigned_request_is_rejected(client):
    body = _pr_body()
    resp = client.post("/webhooks/github", content=body, headers=_headers(body, secret=None))
    assert resp.status_code == 401
    assert client.services.queue.completed == []


def test_wrong_signature_is_rejected(client):
    body = _pr_body()
    resp = client.post("/webhooks/github", content=body, headers=_headers(body, secret="not-the-secret"))
    assert resp.status_code == 401


def test_signature_is_checked_before_the_body_is_parsed(client):
    """Invariant I1: an unauthenticated caller learns nothing about how we parse."""
    body = b"{ this is not json"
    resp = client.post("/webhooks/github", content=body, headers=_headers(body, secret="wrong"))
    assert resp.status_code == 401


def test_malformed_but_signed_json_is_a_client_error(client):
    body = b"{ this is not json"
    resp = client.post("/webhooks/github", content=body, headers=_headers(body))
    assert resp.status_code == 400  # authenticated sender, bad request: never a 500


def test_missing_secret_fails_closed(make_services):
    services = make_services(make_settings(github_webhook_secret=""))
    with TestClient(create_app(services)) as client:
        body = _pr_body()
        resp = client.post("/webhooks/github", content=body, headers=_headers(body))
    assert resp.status_code == 503


def test_signed_pull_request_is_queued_and_reviewed(client):
    services = client.services
    sha = "c" * 40
    services.github.add_pull("acme/shop", 7, sha, files_from(SQLI_DIFF), title="Add user lookup")
    body = _pr_body(sha)
    resp = client.post("/webhooks/github", content=body, headers=_headers(body))
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    # A repository seen for the first time is indexed in the background; the review does not wait for it.
    assert sorted(j["job"] for j in services.queue.completed) == ["index_repository", "review_pull_request"]
    reviews = client.get("/api/reviews").json()["reviews"]
    assert len(reviews) == 1 and reviews[0]["head_sha"] == sha


def test_redelivery_with_same_delivery_id_is_dropped(client):
    services = client.services
    sha = "d" * 40
    services.github.add_pull("acme/shop", 7, sha, files_from(SQLI_DIFF))
    body = _pr_body(sha)
    first = client.post("/webhooks/github", content=body, headers=_headers(body, delivery="same"))
    second = client.post("/webhooks/github", content=body, headers=_headers(body, delivery="same"))
    assert first.status_code == 202
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert [j["job"] for j in services.queue.completed].count("review_pull_request") == 1


def test_same_commit_under_a_new_delivery_id_does_not_review_twice(client):
    """A manual "Redeliver" gets a fresh delivery id; the commit is still reviewed once."""
    services = client.services
    sha = "e" * 40
    services.github.add_pull("acme/shop", 7, sha, files_from(SQLI_DIFF))
    body = _pr_body(sha)
    client.post("/webhooks/github", content=body, headers=_headers(body, delivery="one"))
    client.post("/webhooks/github", content=body, headers=_headers(body, delivery="two"))
    assert len(client.get("/api/reviews").json()["reviews"]) == 1
    assert len(services.store.hitl) <= 1


def test_events_that_are_not_triggers_are_acknowledged_and_ignored(client):
    body = json.dumps({"action": "created", "repository": {"full_name": "acme/shop"}}).encode()
    resp = client.post("/webhooks/github", content=body, headers=_headers(body, event="star"))
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"

    closed = _pr_body(action="closed")
    resp = client.post("/webhooks/github", content=closed, headers=_headers(closed, delivery="d-2"))
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    assert client.services.queue.completed == []


def test_ping_is_answered(client):
    body = json.dumps({"zen": "Keep it logically awesome."}).encode()
    resp = client.post("/webhooks/github", content=body, headers=_headers(body, event="ping"))
    assert resp.status_code == 200 and resp.json()["status"] == "ping"


def test_oversized_payload_is_refused(make_services):
    services = make_services(make_settings(max_webhook_bytes=200))
    with TestClient(create_app(services)) as client:
        body = _pr_body()
        resp = client.post("/webhooks/github", content=body, headers=_headers(body))
    assert resp.status_code == 413


def test_queue_outage_releases_the_claim_so_github_can_retry(client):
    services = client.services
    sha = "f" * 40
    services.github.add_pull("acme/shop", 7, sha, files_from(SQLI_DIFF))
    body = _pr_body(sha)
    original = services.queue.enqueue

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("redis is down")

    services.queue.enqueue = broken  # type: ignore[method-assign]
    assert client.post("/webhooks/github", content=body, headers=_headers(body, delivery="x")).status_code == 503
    services.queue.enqueue = original  # type: ignore[method-assign]
    # The redelivery of the very same delivery id is accepted, not treated as a duplicate.
    assert client.post("/webhooks/github", content=body, headers=_headers(body, delivery="x")).status_code == 202


def test_every_delivery_leaves_an_event(client):
    body = json.dumps({"zen": "hi"}).encode()
    client.post("/webhooks/github", content=body, headers=_headers(body, event="ping", delivery="evt"))
    ingress = [e for e in client.services.store.events if e["agent"] == "ingress"]
    assert ingress and ingress[-1]["payload"]["delivery_id"] == "evt"


def test_draft_pull_requests_are_skipped_by_default(client):
    payload = sample_payload("acme/shop", 9, "1" * 40)
    payload["pull_request"]["draft"] = True
    body = json.dumps(payload).encode()
    resp = client.post("/webhooks/github", content=body, headers=_headers(body))
    assert resp.json()["status"] == "ignored"


def test_repositories_outside_the_allow_list_are_ignored(make_services):
    services = make_services(make_settings(allowed_repos="acme/*"))
    with TestClient(create_app(services)) as client:
        body = json.dumps(sample_payload("evil/repo", 1, "2" * 40)).encode()
        resp = client.post("/webhooks/github", content=body, headers=_headers(body))
    assert resp.json()["status"] == "ignored"


def test_verify_signature_unit():
    from backend.core.exceptions import ConfigurationError, SignatureError

    body = b"payload"
    verify_signature("s", body, compute_signature("s", body))
    with pytest.raises(SignatureError):
        verify_signature("s", body, compute_signature("s", b"tampered"))
    with pytest.raises(SignatureError):
        verify_signature("s", body, "md5=abc")
    with pytest.raises(ConfigurationError):
        verify_signature("", body, compute_signature("", body))
