"""The category vocabulary: one fixed set of labels per specialist.

Labels are how findings are grouped everywhere downstream: the learning loop
calibrates by (repo, agent, category), the evaluator matches by category, and
the dashboard filters by it. If the model invents a fresh label each time
("bare-except", "silent-failure", "error-handling" for the same problem),
feedback never gathers enough evidence to act on and the rule packs' leads
cannot be matched with what the model reported.

So each specialist is shown its labels in the prompt, and whatever the model
returns is normalised here: exact label, known synonym, or, failing both, a
cleaned-up kebab-case label of its own (new kinds of problems still get
through; they just start their own bucket).
"""

from __future__ import annotations

import re

from backend.models.enums import AgentType

CATEGORIES: dict[AgentType, tuple[tuple[str, str], ...]] = {
    AgentType.SECURITY: (
        ("sql-injection", "untrusted input reaches a SQL statement"),
        ("command-injection", "untrusted input reaches a shell or process call"),
        ("code-injection", "eval/exec or template rendering of untrusted input"),
        ("path-traversal", "untrusted input picks a file path"),
        ("ssrf", "untrusted input picks a URL the server fetches"),
        ("xss", "untrusted input rendered into HTML without escaping"),
        ("unsafe-deserialization", "pickle/yaml.load/etc. on untrusted bytes"),
        ("hardcoded-secret", "a credential or key committed to the repository"),
        ("broken-authentication", "missing or bypassable authentication"),
        ("broken-access-control", "missing or wrong authorisation check"),
        ("jwt-verification-disabled", "tokens accepted without signature verification"),
        ("tls-verification-disabled", "certificate verification turned off"),
        ("weak-password-hash", "passwords hashed with a fast or unsalted hash"),
        ("weak-cryptography", "other weak hashing, ciphers or randomness"),
        ("sensitive-data-exposure", "secrets or personal data logged or returned"),
        ("debug-mode-enabled", "debug mode or verbose errors in production code"),
        ("prompt-injection", "text in the change addressed to an AI reviewer or agent"),
    ),
    AgentType.QUALITY: (
        ("logic-error", "the code does not do what it intends"),
        ("swallowed-exception", "an exception is caught and silently ignored"),
        ("error-handling", "errors handled wrongly in another way (too broad, lost context, wrong type)"),
        ("mutable-default-argument", "a mutable default shared across calls"),
        ("unclosed-file", "a file opened without a context manager or close"),
        ("resource-leak", "connections, locks or other resources not released"),
        ("race-condition", "unsynchronised shared state or check-then-act"),
        ("blocking-call-in-async", "a blocking call inside async code"),
        ("null-handling", "None/null not handled where it can occur"),
        ("none-comparison", "comparing to None with == instead of is"),
        ("possible-division-by-zero", "a divisor that can be zero"),
        ("todo-marker", "a TODO/FIXME added in place of finished work"),
        ("performance", "avoidable quadratic work, N+1 queries, needless copies"),
        ("leftover-debugger", "breakpoints or debugger statements left in"),
        ("debug-output", "print or console logging left in"),
        ("dead-code", "unreachable or unused code added"),
        ("complexity", "logic hard enough to follow that it will hide bugs"),
        ("naming", "a name that misleads about what the code does"),
        ("duplication", "logic copied that should be shared"),
    ),
    AgentType.TESTS: (
        ("missing-tests", "new or changed behaviour without tests"),
        ("missing-edge-case-tests", "tests cover the happy path only"),
        ("assertion-free-test", "a test that cannot fail"),
        ("weak-assertion", "assertions too loose to catch a regression"),
        ("removed-assertions", "assertions deleted from existing tests"),
        ("skipped-test", "a test disabled or skipped"),
        ("sleep-in-test", "a fixed sleep used to wait for something"),
        ("flaky-test", "real time, network or order dependence"),
        ("test-isolation", "tests share state or depend on each other"),
    ),
    AgentType.DOCS: (
        ("missing-docstring", "a public function, class or module without a docstring"),
        ("incomplete-api-documentation", "a docstring that omits parameters, return values or errors"),
        ("outdated-documentation", "docs or comments that no longer match the code"),
        ("undocumented-configuration", "a new setting or environment variable not documented"),
        ("missing-changelog", "a user-visible change with no changelog or release note"),
        ("misleading-comment", "a comment that says something the code does not do"),
    ),
}

# Labels models commonly use for the same problems. Keys are already kebab-case.
SYNONYMS: dict[str, str] = {
    # security
    "sqli": "sql-injection", "sql-injection-risk": "sql-injection", "unsafe-sql": "sql-injection",
    "sql-string-interpolation": "sql-injection", "shell-injection": "command-injection",
    "os-command-injection": "command-injection", "rce": "code-injection", "eval-injection": "code-injection",
    "insecure-deserialization": "unsafe-deserialization", "pickle-deserialization": "unsafe-deserialization",
    "hardcoded-credential": "hardcoded-secret", "hardcoded-credentials": "hardcoded-secret",
    "hard-coded-secret": "hardcoded-secret", "secret-exposure": "hardcoded-secret", "leaked-secret": "hardcoded-secret",
    "exposed-api-key": "hardcoded-secret", "hardcoded-api-key": "hardcoded-secret",
    "insecure-tls": "tls-verification-disabled", "ssl-verification-disabled": "tls-verification-disabled",
    "certificate-verification-disabled": "tls-verification-disabled", "insecure-transport": "tls-verification-disabled",
    "directory-traversal": "path-traversal", "cross-site-scripting": "xss", "weak-crypto": "weak-cryptography",
    "weak-hash": "weak-cryptography", "insecure-password-hash": "weak-password-hash", "insecure-randomness": "weak-cryptography",
    "missing-authorization": "broken-access-control", "authorization": "broken-access-control",
    "missing-authentication": "broken-authentication", "information-disclosure": "sensitive-data-exposure",
    "ai-instruction-injection": "prompt-injection", "llm-prompt-injection": "prompt-injection",
    # quality
    "bare-except": "swallowed-exception", "silent-exception": "swallowed-exception",
    "silently-swallowed-exception": "swallowed-exception", "exception-swallowing": "swallowed-exception",
    "silent-failure": "swallowed-exception", "ignored-exception": "swallowed-exception",
    "swallowed-error": "swallowed-exception", "suppressed-exception": "swallowed-exception",
    "empty-except": "swallowed-exception", "broad-exception": "error-handling", "overly-broad-except": "error-handling",
    "exception-handling": "error-handling", "mutable-default": "mutable-default-argument",
    "mutable-default-arg": "mutable-default-argument", "mutable-default-parameter": "mutable-default-argument",
    "shared-mutable-default": "mutable-default-argument", "bug": "logic-error", "correctness": "logic-error",
    "off-by-one": "logic-error", "resource-management": "resource-leak", "file-not-closed": "unclosed-file",
    "unclosed-resource": "resource-leak", "concurrency": "race-condition", "thread-safety": "race-condition",
    "none-handling": "null-handling", "null-dereference": "null-handling", "debugger-statement": "leftover-debugger",
    "print-statement": "debug-output", "leftover-print": "debug-output", "maintainability": "complexity",
    "readability": "complexity", "code-duplication": "duplication",
    # tests
    "missing-test": "missing-tests", "no-tests": "missing-tests", "untested-code": "missing-tests",
    "test-coverage": "missing-tests", "missing-test-coverage": "missing-tests", "insufficient-tests": "missing-tests",
    "missing-edge-cases": "missing-edge-case-tests", "edge-case-coverage": "missing-edge-case-tests",
    "test-without-assertions": "assertion-free-test", "no-assertions": "assertion-free-test",
    "missing-assertions": "assertion-free-test", "missing-assertion": "assertion-free-test",
    "test-has-no-assertions": "assertion-free-test", "ineffective-test": "assertion-free-test",
    "vacuous-test": "assertion-free-test", "weak-test": "weak-assertion", "weak-assertions": "weak-assertion",
    "flaky": "flaky-test", "time-sleep-in-test": "sleep-in-test", "disabled-test": "skipped-test",
    # docs
    "missing-documentation": "missing-docstring", "undocumented-function": "missing-docstring",
    "missing-docstrings": "missing-docstring", "no-docstring": "missing-docstring",
    "undocumented-public-api": "missing-docstring", "documentation": "missing-docstring",
    "incomplete-docstring": "incomplete-api-documentation", "incomplete-documentation": "incomplete-api-documentation",
    "stale-documentation": "outdated-documentation", "stale-comment": "outdated-documentation",
    "outdated-docs": "outdated-documentation", "undocumented-config": "undocumented-configuration",
    "undocumented-env-var": "undocumented-configuration", "changelog": "missing-changelog",
}  # fmt: skip

_NON_LABEL = re.compile(r"[^a-z0-9]+")


def kebab(raw: str) -> str:
    return _NON_LABEL.sub("-", raw.strip().lower()).strip("-")[:60] or "uncategorised"


def labels(agent: AgentType) -> set[str]:
    return {label for label, _ in CATEGORIES.get(agent, ())}


def normalise(agent: AgentType, raw: str) -> str:
    """Map a model's label onto the agent's vocabulary when it means the same thing."""
    label = kebab(raw)
    if label in labels(agent):
        return label
    mapped = SYNONYMS.get(label)
    if mapped is not None:
        return mapped
    # A known label with a trailing qualifier ("sql-injection-risk-in-query") is still that label.
    for known in sorted(labels(agent), key=len, reverse=True):
        if label.startswith(known + "-"):
            return known
    return label


def prompt_section(agent: AgentType) -> str:
    rows = "\n".join(f"- `{label}`: {meaning}" for label, meaning in CATEGORIES.get(agent, ()))
    return (
        "## Category labels\n"
        "Set `category` to one of these labels whenever one fits. Invent a new kebab-case label only for a "
        "problem none of them describes.\n" + rows
    )
