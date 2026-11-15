"""Exception taxonomy.

The split that matters operationally is *retryable* versus *not*: retry and
circuit-breaker code branches on it, so every boundary adapter (LLM, GitHub,
embeddings, database) translates its native errors into one of these.
"""

from __future__ import annotations


class ReviewAgentError(Exception):
    """Base class for every error this system raises on purpose."""

    retryable: bool = False


class RetryableError(ReviewAgentError):
    """Transient failure: timeouts, 429s, 5xx, connection resets."""

    retryable = True


class PermanentError(ReviewAgentError):
    """Retrying cannot help: bad credentials, 4xx, malformed input."""


class CircuitOpenError(ReviewAgentError):
    """A dependency is failing fast; callers should degrade, not wait."""

    def __init__(self, name: str, retry_after_s: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_after_s:.1f}s")
        self.name = name
        self.retry_after_s = retry_after_s


class NodeTimeoutError(ReviewAgentError):
    """A workflow node exceeded its time budget."""

    def __init__(self, label: str, seconds: float) -> None:
        super().__init__(f"{label} timed out after {seconds:.1f}s")
        self.label = label
        self.seconds = seconds


class BudgetExceededError(ReviewAgentError):
    """The cost cap was hit before the call was made."""


class SignatureError(PermanentError):
    """Webhook payload failed HMAC verification."""


class OutputValidationError(ReviewAgentError):
    """A model returned something that does not satisfy the Finding contract."""


class ConfigurationError(PermanentError):
    """The deployment is missing something it needs (keys, schema, extension)."""
