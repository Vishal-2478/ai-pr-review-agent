"""Embedders: text -> vector.

``OpenAIEmbedder`` is the production path (text-embedding-3-large reduced to
256 dimensions, which keeps the DiskANN index small). ``HashEmbedder`` is a
deterministic, dependency-free stand-in so that retrieval, the demo and the
test-suite all work without an API key. It captures lexical overlap only; do
not expect semantic recall from it.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Any, Protocol

from backend.core.exceptions import PermanentError, RetryableError
from backend.economics.pricing import cost_usd, estimate_tokens
from backend.models.enums import EventType
from backend.reliability.circuit_breaker import CircuitBreaker
from backend.reliability.retry import is_retryable, retry_async

MAX_EMBED_CHARS = 24_000  # stay well inside the model's input limit
BATCH_SIZE = 64


class Embedder(Protocol):
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def code_tokens(text: str) -> list[str]:
    """Identifiers split on snake_case and camelCase boundaries, lower-cased."""
    tokens: list[str] = []
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", text):
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", ident).replace("_", " ").lower().split()
        tokens.extend(parts)
        if len(parts) > 1:
            tokens.append(ident.lower())
    return tokens


class HashEmbedder:
    """Feature hashing with signed buckets, L2-normalised."""

    model = "hash-embedding"

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        tokens = code_tokens(text)
        grams = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        counts: dict[str, int] = {}
        for g in grams:
            counts[g] = counts.get(g, 0) + 1
        for gram, count in counts.items():
            digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class OpenAIEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str,
        dimensions: int,
        *,
        base_url: str = "",
        emitter: Any | None = None,
        breaker: CircuitBreaker | None = None,
        retry_attempts: int = 3,
        timeout: float = 30.0,
    ) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, max_retries=0)
        self.model = model
        self.dimensions = dimensions
        self._emitter = emitter
        self._breaker = breaker or CircuitBreaker("embeddings")
        self._retry_attempts = retry_attempts
        self._timeout = timeout

    def attach_emitter(self, emitter: Any) -> None:
        self._emitter = emitter

    async def _batch(self, batch: list[str]) -> tuple[list[list[float]], int]:
        import openai

        try:
            resp = await self._client.embeddings.create(
                model=self.model, input=batch, dimensions=self.dimensions, timeout=self._timeout
            )
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as exc:
            raise RetryableError(f"{type(exc).__name__}: {exc}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code == 429:
                raise RetryableError(f"HTTP {exc.status_code}: {exc}") from exc
            raise PermanentError(f"HTTP {exc.status_code}: {exc}") from exc
        vectors = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        tokens = getattr(resp.usage, "prompt_tokens", 0) or sum(estimate_tokens(t) for t in batch)
        return vectors, tokens

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = [(t or " ")[:MAX_EMBED_CHARS] for t in texts[i : i + BATCH_SIZE]]
            started = time.perf_counter()
            vectors, tokens = await retry_async(
                lambda b=batch: self._breaker.call(lambda: self._batch(b), counts_as_failure=is_retryable),
                attempts=self._retry_attempts,
            )
            out.extend(vectors)
            if self._emitter is not None:
                await self._emitter.emit(
                    EventType.LLM_CALL, agent="embedder", name="embed", model=self.model, outcome="ok",
                    tokens_in=tokens, cost_usd=cost_usd(self.model, tokens),
                    latency_ms=int((time.perf_counter() - started) * 1000), texts=len(batch),
                )  # fmt: skip
        return out
