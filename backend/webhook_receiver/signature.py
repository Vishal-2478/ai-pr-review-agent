"""Webhook authenticity: HMAC-SHA256 over the raw request body.

Invariant I1: nothing in a webhook payload is read, parsed or acted on until
this check has passed. The comparison is constant-time, and a missing secret
fails closed: an unsigned endpoint would let anyone on the internet spend the
model budget and post reviews.
"""

from __future__ import annotations

import hashlib
import hmac

from backend.core.exceptions import ConfigurationError, SignatureError

SIGNATURE_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    return _PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> None:
    if not secret:
        raise ConfigurationError("GITHUB_WEBHOOK_SECRET is not set; refusing to accept webhooks")
    if not header or not header.startswith(_PREFIX):
        raise SignatureError("missing or malformed signature header")
    if not hmac.compare_digest(compute_signature(secret, body), header):
        raise SignatureError("signature mismatch")
