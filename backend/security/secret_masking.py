"""Find and mask credentials.

Used in three places: before a diff is sent to a model, before anything is
written to the event log, and before a finding is posted publicly (a comment
that quotes the leaked key would leak it a second time).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----|\Z)")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("connection-string", re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqp)://[^\s:@/'\"]+:([^\s@/'\"]{3,})@")),
    (
        "hardcoded-credential",
        re.compile(
            r"""(?ix)
            \b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key)
            \b["']?\s*[:=]\s*
            (["'])(?!\s*\1)([^"'\s$<{][^"']{5,})\1
            """
        ),
    ),
]  # fmt: skip

_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{3,}|\*{3,}|changeme|change[-_]me|example|placeholder|your[-_ ].*|<.*>|\$\{.*\}|dummy|test|password|secret|none|null|todo)$"
)

# Passwords in a connection string that are defaults or template variables, not
# secrets: `postgres:postgres@localhost` in a compose file is not a leak, and
# escalating it as one teaches people to ignore the reviewer.
_DEFAULT_PASSWORD = re.compile(
    r"(?i)^(?:postgres|password|passwd|pass|root|admin|guest|user|example|changeme|change[-_]me|secret|test|dev|local"
    r"|redis|mysql|mongo|rabbitmq|x{3,}|\*{3,}|<.*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?.*|\{.*\}|%s|your[-_ ].*)$"
)


@dataclass(frozen=True)
class SecretHit:
    kind: str
    line: int  # 1-based line within the scanned text
    start: int
    end: int
    preview: str  # masked, safe to log


def find_secrets(text: str) -> list[SecretHit]:
    hits: list[SecretHit] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text or ""):
            group = 2 if kind == "hardcoded-credential" else (1 if kind == "connection-string" else 0)
            start, end = m.span(group)
            value = m.group(group)
            if kind == "hardcoded-credential" and _PLACEHOLDER.match(value.strip()):
                continue
            if kind == "connection-string" and _DEFAULT_PASSWORD.match(value.strip()):
                continue
            if any(s <= start < e or s < end <= e for s, e in taken):
                continue
            taken.append((start, end))
            hits.append(
                SecretHit(
                    kind=kind,
                    line=text.count("\n", 0, start) + 1,
                    start=start,
                    end=end,
                    preview=_preview(value),
                )
            )
    return sorted(hits, key=lambda h: h.start)


def _preview(value: str) -> str:
    v = value.strip().splitlines()[0] if value.strip() else ""
    return f"{v[:4]}…[{len(value)} chars]" if len(v) > 8 else "…"


def mask(text: str) -> str:
    """Replace every detected secret with a typed placeholder."""
    if not text:
        return text
    hits = find_secrets(text)
    if not hits:
        return text
    out: list[str] = []
    cursor = 0
    for h in hits:
        out.append(text[cursor : h.start])
        out.append(f"[REDACTED:{h.kind}]")
        cursor = h.end
    out.append(text[cursor:])
    return "".join(out)


def mask_obj(obj: object) -> object:
    """Mask strings anywhere inside JSON-like data."""
    if isinstance(obj, str):
        return mask(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v) for v in obj]
    return obj
