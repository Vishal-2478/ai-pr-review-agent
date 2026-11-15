"""Model client boundary.

Specialists talk to :class:`LLMClient`, never to a vendor SDK. Two providers
ship: Claude through the Anthropic Messages API, and OpenAI. The OpenAI
implementation speaks Chat Completions on purpose: it is the dialect every
OpenAI-compatible gateway understands, so pointing OPENAI_BASE_URL at another
provider is enough to route an agent to a cheaper model.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from backend.core.exceptions import OutputValidationError, PermanentError, RetryableError

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    latency_ms: int = 0


class LLMClient(Protocol):
    provider: str

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_output_tokens: int,
        timeout: float,
        tag: str = "",
    ) -> LLMResponse: ...


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating code fences and stray prose."""
    cleaned = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", cleaned)
    if fenced:
        cleaned = fenced.group(1)
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise OutputValidationError("model reply contains no JSON object")
        cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise OutputValidationError(f"model reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise OutputValidationError("model reply must be a JSON object")
    return data


class OpenAIChatClient:
    provider = "openai"

    # Request options a given model or gateway may not accept. When a 400 names
    # one of them it is dropped for that model and the call is repeated once.
    _OPTIONAL = ("temperature", "response_format", "max_completion_tokens")

    def __init__(self, api_key: str, base_url: str = "", temperature: float | None = None) -> None:
        from openai import AsyncOpenAI

        # Retries are handled by our own retry + circuit breaker so they show up on the event spine.
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, max_retries=0)
        self._temperature = temperature
        self._unsupported: dict[str, set[str]] = {}

    async def complete_json(
        self, *, model: str, system: str, user: str, max_output_tokens: int, timeout: float, tag: str = ""
    ) -> LLMResponse:
        import openai

        params: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": max_output_tokens,
            "timeout": timeout,
        }
        if self._temperature is not None:
            params["temperature"] = self._temperature
        for name in self._unsupported.get(model, ()):
            params.pop(name, None)

        for _ in range(len(self._OPTIONAL) + 1):
            started = time.perf_counter()
            try:
                resp = await self._client.chat.completions.create(**params)
            except openai.BadRequestError as exc:
                rejected = next((p for p in self._OPTIONAL if p in params and p in str(exc)), None)
                if rejected is None:
                    raise PermanentError(f"model rejected the request: {exc}") from exc
                log.info("model %s does not accept '%s'; dropping it", model, rejected)
                self._unsupported.setdefault(model, set()).add(rejected)
                params.pop(rejected)
                continue
            except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as exc:
                raise RetryableError(f"{type(exc).__name__}: {exc}") from exc
            except openai.APIStatusError as exc:
                if exc.status_code >= 500 or exc.status_code == 429:
                    raise RetryableError(f"HTTP {exc.status_code}: {exc}") from exc
                raise PermanentError(f"HTTP {exc.status_code}: {exc}") from exc
            usage = resp.usage
            return LLMResponse(
                text=resp.choices[0].message.content or "",
                tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                tokens_out=getattr(usage, "completion_tokens", 0) or 0,
                model=model,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        raise PermanentError("model rejected every request variant")  # pragma: no cover


class AnthropicClient:
    """Claude through the Messages API, spoken over plain HTTP.

    No SDK on purpose: timeouts, retries and the circuit breaker are ours, so
    every attempt is visible on the event spine, and the request shape is the
    small stable core of the API (system + one user message).
    """

    provider = "anthropic"
    API_VERSION = "2023-06-01"

    def __init__(
        self, api_key: str, base_url: str = "", temperature: float | None = None, client: Any | None = None
    ) -> None:
        import httpx

        self._client = client or httpx.AsyncClient(
            base_url=(base_url or "https://api.anthropic.com").rstrip("/"),
            headers={"x-api-key": api_key, "anthropic-version": self.API_VERSION, "content-type": "application/json"},
        )
        self._temperature = temperature

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete_json(
        self, *, model: str, system: str, user: str, max_output_tokens: int, timeout: float, tag: str = ""
    ) -> LLMResponse:
        import httpx

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        started = time.perf_counter()
        try:
            resp = await self._client.post("/v1/messages", json=body, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:  # includes 529 "overloaded"
            raise RetryableError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise PermanentError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            text = "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text")
            usage = data.get("usage") or {}
        except (ValueError, KeyError, TypeError) as exc:
            raise RetryableError(f"unreadable response from the Messages API: {exc}") from exc
        if data.get("stop_reason") == "max_tokens":
            log.warning("model %s stopped at max_tokens; the reply may be truncated JSON", model)
        return LLMResponse(
            text=text,
            tokens_in=int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            model=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


Scripted = str | dict[str, Any] | BaseException | Callable[[str, str, str], Any]


class ScriptedLLMClient:
    """Deterministic stand-in used by tests and fault-injection drills.

    ``script`` maps a tag (the agent name) to a reply or a list of replies used
    in order. A reply may be a JSON string, a dict, an exception to raise, or a
    callable ``(model, system, user) -> reply`` (sync or async).

    By default it behaves like a model that follows the prompt contract: a dict
    reply without ``dismissed_signals`` dismisses every pre-check signal (S1,
    S2, ...) in the prompt. Pass ``rule_on_signals=False`` to imitate a model
    that ignores them.
    """

    provider = "scripted"
    _SIGNAL = re.compile(r"^- (S\d+): ", re.MULTILINE)

    def __init__(
        self,
        script: dict[str, Scripted | list[Scripted]] | None = None,
        default: Scripted | None = None,
        rule_on_signals: bool = True,
    ) -> None:
        self.script = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (script or {}).items()}
        self.default: Scripted = default if default is not None else {"findings": []}
        self.rule_on_signals = rule_on_signals
        self.calls: list[dict[str, str]] = []

    async def complete_json(
        self, *, model: str, system: str, user: str, max_output_tokens: int, timeout: float, tag: str = ""
    ) -> LLMResponse:
        self.calls.append({"tag": tag, "model": model, "system": system, "user": user})
        queue = self.script.get(tag)
        reply: Any = (queue.pop(0) if len(queue) > 1 else queue[0]) if queue else self.default
        if callable(reply) and not isinstance(reply, BaseException):
            reply = reply(model, system, user)
            if hasattr(reply, "__await__"):
                reply = await reply
        if isinstance(reply, BaseException):
            raise reply
        if self.rule_on_signals and isinstance(reply, dict) and "dismissed_signals" not in reply:
            reply = {**reply, "dismissed_signals": [
                {"signal": sid, "reason": "scripted: not a problem here"} for sid in self._SIGNAL.findall(user)
            ]}  # fmt: skip
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text=text, tokens_in=len(system + user) // 4, tokens_out=len(text) // 4, model=model, latency_ms=1)
