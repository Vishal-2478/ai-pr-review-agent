"""M6: semantic memory. Chunking, embedding, hybrid retrieval, incremental indexing."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.data.chunker import chunk_file
from backend.integrations.diff import parse_unified_diff
from backend.job_queue import jobs
from backend.memory.context_retriever import extract_identifiers, identifier_terms, reciprocal_rank_fusion
from backend.memory.embedder import HashEmbedder, code_tokens
from backend.models.schemas import RetrievedChunk
from backend.tools.llm_client import ScriptedLLMClient
from tests.conftest import open_pull

PAYMENTS = '''"""Payments."""
import decimal

RATE = decimal.Decimal("0.2")


def charge_customer(customer_id, amount_cents):
    """Charge a customer and return the receipt id."""
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    return gateway.charge(customer_id, amount_cents)


class RefundPolicy:
    """Refund rules."""

    def can_refund(self, order):
        return order.age_days < 30
'''

CALLER_DIFF = """diff --git a/app/checkout.py b/app/checkout.py
index 1111111..2222222 100644
--- a/app/checkout.py
+++ b/app/checkout.py
@@ -1,4 +1,8 @@
 from app.payments import charge_customer

+def checkout(cart, customer_id):
+    \"\"\"Charge the customer for the cart.\"\"\"
+    return charge_customer(customer_id, cart.total_cents * -1)
+
 def noop():
     return None
"""


def test_python_is_chunked_on_symbol_boundaries():
    chunks = chunk_file("acme/shop", "app/payments.py", PAYMENTS)
    symbols = {c.symbol for c in chunks}
    assert {"charge_customer", "RefundPolicy"} <= symbols or any("charge_customer" in (c.symbol or "") for c in chunks)
    fn = next(c for c in chunks if "charge_customer" in (c.symbol or ""))
    assert "def charge_customer" in fn.content and "class RefundPolicy" not in fn.content
    assert fn.start_line <= 7 <= fn.end_line
    assert all(c.repo == "acme/shop" and c.path == "app/payments.py" and c.token_count > 0 for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_chunking_survives_unparseable_and_non_python_files():
    assert chunk_file("r", "broken.py", "def broken(:\n    pass\n" * 5)
    js = chunk_file("r", "src/app.js", "\n".join(f"function f{i}() {{ return {i}; }}" for i in range(200)))
    assert len(js) > 1 and all(c.end_line >= c.start_line for c in js)
    assert chunk_file("r", "empty.py", "") == []


async def test_hash_embedder_is_deterministic_normalised_and_lexically_meaningful():
    embedder = HashEmbedder(256)
    a, b, c = await embedder.embed(["def charge_customer(customer_id, amount_cents)", "charge_customer(customer_id, total)", "render html template footer"])
    assert len(a) == 256 and abs(sum(x * x for x in a) - 1.0) < 1e-6
    assert (await embedder.embed(["def charge_customer(customer_id, amount_cents)"]))[0] == a

    def cos(u, v):
        return sum(x * y for x, y in zip(u, v, strict=True))

    assert cos(a, b) > cos(a, c)
    assert "charge" in code_tokens("chargeCustomer") and "customer" in code_tokens("charge_customer")


def test_identifiers_come_from_the_changed_lines():
    files = parse_unified_diff(CALLER_DIFF)
    identifiers = extract_identifiers(files)
    assert "charge_customer" in identifiers and "checkout" in identifiers
    assert identifier_terms("charge_customer") == ["charge", "customer"]
    # Postgres full-text search keeps camelCase as one token, so the query term must too.
    assert identifier_terms("totalCents") == ["totalcents"]


def test_reciprocal_rank_fusion_rewards_agreement():
    def chunk(i: str) -> RetrievedChunk:
        return RetrievedChunk(id=i, repo="r", path=f"{i}.py", content=i, start_line=1, end_line=2)

    vector = [chunk("a"), chunk("b"), chunk("c")]
    lexical = [chunk("c"), chunk("a"), chunk("d")]
    fused = [c.id for c in reciprocal_rank_fusion([vector, lexical])]
    assert fused[:2] == ["a", "c"] and set(fused) == {"a", "b", "c", "d"}  # in both lists beats top of one


async def test_review_retrieves_the_definition_of_what_the_diff_calls(make_services, tmp_path: Path):
    """The point of RAG here: the diff calls charge_customer; the reviewer sees its contract."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "payments.py").write_text(PAYMENTS)
    (tmp_path / "app" / "unrelated.py").write_text('"""Unrelated."""\n\ndef render_footer():\n    return "<footer/>"\n')
    (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\n\n- Never pass negative amounts to the payment gateway.\n")

    llm = ScriptedLLMClient()
    services = make_services(llm=llm)
    report = await services.indexer.index_local("acme/shop", tmp_path)
    assert report.indexed >= 2 and report.chunks >= 3

    await jobs.review_pull_request(services, open_pull(services, CALLER_DIFF))
    prompt = llm.calls[0]["user"]
    assert "def charge_customer" in prompt and "amount must be positive" in prompt
    assert "Never pass negative amounts" in prompt  # procedural memory: the team's conventions
    if "render_footer" in prompt:  # a three-file corpus fits in top-k; relevance still has to win
        assert prompt.index("def charge_customer") < prompt.index("def render_footer")
    retrieval = [e for e in services.store.events if e["name"] == "retrieve_context" and e["event_type"] == "span.end"]
    assert retrieval and retrieval[-1]["outcome"] == "ok"


async def test_reindexing_only_touches_changed_files(make_services, tmp_path: Path):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")
    services = make_services()
    first = await services.indexer.index_local("acme/shop", tmp_path)
    assert first.indexed == 2

    second = await services.indexer.index_local("acme/shop", tmp_path)
    assert second.indexed == 0 and second.unchanged == 2  # nothing changed: nothing re-embedded

    (tmp_path / "a.py").write_text("def a():\n    return 100\n")
    (tmp_path / "b.py").unlink()
    third = await services.indexer.index_local("acme/shop", tmp_path)
    assert third.indexed == 1 and third.removed == 1
    stats = await services.store.index_stats("acme/shop")
    assert stats["files"] == 1


async def test_review_still_runs_when_retrieval_is_down(make_services):
    """Degrade, do not fail: no index and a broken search still produce a (flagged) review."""
    services = make_services(llm=ScriptedLLMClient())

    async def broken(*args, **kwargs):
        raise ConnectionError("vector index unavailable")

    services.store.search_vector = broken  # type: ignore[method-assign]
    services.store.search_lexical = broken  # type: ignore[method-assign]
    result = await jobs.review_pull_request(services, open_pull(services))
    review = await services.store.get_review(result["review_id"])
    assert review["status"] in ("posted", "pending_human", "clean")
    spans = [e for e in services.store.events if e["name"] == "retrieve_context" and e["event_type"] == "span.end"]
    assert spans[-1]["outcome"] == "degraded"


@pytest.mark.parametrize("path,expected", [("node_modules/x/index.js", False), ("app/main.py", True), ("dist/bundle.min.js", False), ("logo.png", False)])
async def test_indexer_skips_vendored_and_binary_files(make_services, tmp_path: Path, path: str, expected: bool):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def main():\n    return 1\n")
    services = make_services()
    report = await services.indexer.index_local("acme/shop", tmp_path)
    assert (report.indexed == 1) is expected
