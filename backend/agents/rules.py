"""Deterministic rule packs, one per specialist.

They do three jobs:

1. **Pre-check signals.** Before a model is called, the rules run over the
   diff and their hits are handed to the model as leads to verify.
2. **Rules fallback.** If the model is unreachable, over budget or times out,
   the specialist degrades to its rule pack instead of returning nothing.
3. **Offline mode.** With no API key (``LLM_PROVIDER=rules``) the whole system
   still runs end to end, which is what the demo, CI and golden-set tests use.

Rules only ever look at lines a pull request adds (plus nearby context), and
each hit quotes the offending line as evidence, so rule findings are grounded
by construction. Confidence values are deliberately conservative for anything
a pattern cannot fully judge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.agents.contracts import Finding
from backend.integrations.diff import ParsedPatch, is_doc_path, is_source_path, is_test_path, parse_patch
from backend.models.enums import AgentType, Severity
from backend.models.schemas import DiffFile
from backend.security.injection_guard import scan_untrusted
from backend.security.secret_masking import find_secrets

PY = (".py",)
JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


@dataclass(frozen=True)
class LineRule:
    category: str
    severity: Severity
    confidence: float
    pattern: re.Pattern[str]
    summary: str
    rationale: str
    suggestion: str
    suffixes: tuple[str, ...] = ()
    include_tests: bool = False


def _r(p: str) -> re.Pattern[str]:
    return re.compile(p)


SECURITY_RULES: tuple[LineRule, ...] = (
    LineRule(
        "sql-injection", Severity.HIGH, 0.8,
        _r(r"""\b(?:execute|executemany|executescript|raw|read_sql|read_sql_query)\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\+|\.format\s*\())"""),
        "SQL statement is built by interpolating values into the query string",
        "String-built SQL lets any value that reaches this call change the statement itself. If any part of it comes from a request, this is SQL injection.",
        "Pass values as bound parameters (e.g. execute(\"... WHERE id = %s\", (value,))) and keep the SQL text constant.",
        PY,
    ),
    LineRule(
        "sql-injection", Severity.HIGH, 0.65,
        _r(r"""(?i)f["'](?:[^"']*\b(?:select\s.+\sfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)\b[^"']*\{[^}]+\})"""),
        "SQL text is assembled with an f-string",
        "Interpolating into SQL text bypasses the driver's parameter binding, so the interpolated value can alter the query.",
        "Use a parameterised query and pass the value separately.",
        PY,
    ),
    LineRule(
        "sql-injection", Severity.HIGH, 0.75,
        _r(r"""\.(?:query|execute|raw)\s*\(\s*(?:`[^`]*\$\{|["'][^"']*["']\s*\+)"""),
        "SQL statement is built by string interpolation",
        "Template literals and concatenation put raw values into the SQL text, which allows injection.",
        "Use placeholders ($1, ?) and pass the values as the parameter array.",
        JS,
    ),
    LineRule(
        "command-injection", Severity.HIGH, 0.75,
        _r(r"""\bsubprocess\.\w+\([^)]*shell\s*=\s*True|\bos\.(?:system|popen)\s*\(\s*(?:f["']|[^"')]*["']\s*(?:\+|%)|[a-zA-Z_])"""),
        "Shell command is executed with shell interpretation of a dynamic string",
        "With shell=True (or os.system) any shell metacharacter in the command string is executed. Dynamic input here becomes command injection.",
        "Pass an argument list without shell=True, e.g. subprocess.run([\"git\", \"log\", ref], check=True).",
        PY,
    ),
    LineRule(
        "command-injection", Severity.HIGH, 0.7,
        _r(r"""\b(?:exec|execSync)\s*\(\s*(?:`[^`]*\$\{|["'][^"']*["']\s*\+)"""),
        "Shell command is built by string interpolation",
        "child_process.exec runs its argument through a shell, so interpolated input can inject commands.",
        "Use execFile/spawn with an argument array.",
        JS,
    ),
    LineRule(
        "code-injection", Severity.HIGH, 0.65,
        _r(r"""(?<![\w.])(?:eval|exec)\s*\(\s*(?!["'])"""),
        "Dynamic code execution on a non-literal value",
        "eval/exec runs whatever string it is given with the full privileges of the process.",
        "Replace with an explicit parser or dispatch table (ast.literal_eval for literals, JSON.parse for data).",
        PY + JS,
    ),
    LineRule(
        "unsafe-deserialization", Severity.HIGH, 0.7,
        _r(r"""\b(?:pickle|cPickle|marshal|dill)\.loads?\s*\(|\byaml\.load\s*\((?![^)\n]*SafeLoader)"""),
        "Deserialising data with a format that can execute code",
        "pickle and yaml.load construct arbitrary objects; untrusted input leads to remote code execution.",
        "Use json, or yaml.safe_load, for anything that crosses a trust boundary.",
        PY,
    ),
    LineRule(
        "tls-verification-disabled", Severity.MEDIUM, 0.8,
        _r(r"""\bverify\s*=\s*False\b|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|\bCERT_NONE\b"""),
        "TLS certificate verification is disabled",
        "Without certificate verification the connection can be intercepted and modified by anyone on the path.",
        "Remove the override; if a private CA is in use, pass its bundle instead.",
    ),
    LineRule(
        "weak-password-hash", Severity.MEDIUM, 0.7,
        _r(r"""(?i)(?=.*(?:passw|pwd|secret|token)).*\bhashlib\.(?:md5|sha1)\s*\("""),
        "Fast, broken hash used for a secret",
        "MD5 and SHA-1 are fast to brute-force and unsuitable for passwords or tokens.",
        "Use a password KDF (argon2, bcrypt, scrypt) or hashlib.pbkdf2_hmac with a per-user salt.",
        PY,
    ),
    LineRule(
        "jwt-verification-disabled", Severity.HIGH, 0.8,
        _r(r"""verify_signature["']?\s*:\s*False|algorithms\s*=\s*\[\s*["']none["']|jwt\.decode\([^)\n]*verify\s*=\s*False"""),
        "JWT signature verification is turned off",
        "A token whose signature is not verified can be forged by anyone, so every claim in it is attacker-controlled.",
        "Verify the signature with an explicit algorithm allow-list.",
    ),
    LineRule(
        "debug-mode-enabled", Severity.MEDIUM, 0.55,
        _r(r"""\.run\([^)\n]*\bdebug\s*=\s*True|^\s*DEBUG\s*=\s*True\b"""),
        "Debug mode is enabled in application code",
        "Framework debug modes expose stack traces and, for some frameworks, an interactive console.",
        "Read the flag from configuration and default it to off.",
        PY,
    ),
)

QUALITY_RULES: tuple[LineRule, ...] = (
    LineRule(
        "swallowed-exception", Severity.MEDIUM, 0.7,
        _r(r"""^\s*except\s*:\s*(?:#.*)?$|^\s*except\s+(?:Exception|BaseException)\b[^:]*:\s*(?:pass|continue)\b"""),
        "Exception is caught and discarded",
        "A bare or blanket except hides every failure on this path, including the ones nobody anticipated, and leaves no trace for whoever debugs it.",
        "Catch the specific exception you expect and log or re-raise the rest.",
        PY,
    ),
    LineRule(
        "mutable-default-argument", Severity.MEDIUM, 0.85,
        _r(r"""\bdef\s+\w+\s*\([^)]*=\s*(?:\[\s*\]|\{\s*\}|set\(\)|list\(\)|dict\(\))"""),
        "Mutable default argument is shared between calls",
        "The default object is created once at definition time, so state leaks from one call into the next.",
        "Default to None and create the object inside the function.",
        PY,
    ),
    LineRule(
        "leftover-debugger", Severity.MEDIUM, 0.9,
        _r(r"""\b(?:breakpoint\(\)|i?pdb\.set_trace\(\))|^\s*debugger;?\s*$"""),
        "Debugger statement left in the change",
        "This halts execution and waits for a terminal that will not exist in production.",
        "Remove it.",
        PY + JS,
    ),
    LineRule(
        "none-comparison", Severity.LOW, 0.8,
        _r(r"""[!=]=\s*None\b"""),
        "Comparison to None with == instead of is",
        "== can be overridden by the object and gives surprising results for types that define __eq__.",
        "Use `is None` / `is not None`.",
        PY,
    ),
    LineRule(
        "unclosed-file", Severity.LOW, 0.5,
        _r(r"""(?<![\w.])open\s*\([^)\n]*\)\s*\.(?:read|write|readlines)\s*\("""),
        "File is opened without being closed",
        "The handle is only released when the garbage collector gets to it, which leaks descriptors under load.",
        "Use a `with open(...) as f:` block.",
        PY,
    ),
    LineRule(
        "possible-division-by-zero", Severity.LOW, 0.5,
        _r(r"""/\s*len\s*\("""),
        "Division by a length that can be zero",
        "If the collection is empty this raises ZeroDivisionError.",
        "Guard the empty case explicitly.",
        PY,
    ),
    LineRule(
        "debug-output", Severity.LOW, 0.45,
        _r(r"""^\s*print\s*\(|\bconsole\.(?:log|debug)\s*\("""),
        "Debug output left in the change",
        "Unstructured prints bypass the logger, cannot be filtered, and may leak data.",
        "Use the project's logger or remove the line.",
        PY + JS,
    ),
    LineRule(
        "todo-marker", Severity.INFO, 0.5,
        _r(r"""(?:#|//|/\*)\s*(?:TODO|FIXME|HACK|XXX)\b"""),
        "Unfinished-work marker added",
        "TODOs merged without an owner or ticket tend to stay forever.",
        "Link an issue or finish it in this change.",
    ),
)

TEST_RULES: tuple[LineRule, ...] = (
    LineRule(
        "skipped-test", Severity.LOW, 0.7,
        _r(r"""@pytest\.mark\.(?:skip|xfail)\b|@unittest\.skip|\b(?:it|test|describe)\.skip\s*\(|\bx(?:it|describe)\s*\("""),
        "Test is skipped or expected to fail",
        "A skipped test protects nothing, and skips added to get a change through are rarely removed.",
        "Fix the test, or record why it is skipped and when it will be restored.",
        include_tests=True,
    ),
    LineRule(
        "sleep-in-test", Severity.LOW, 0.6,
        _r(r"""\btime\.sleep\s*\(|\bsetTimeout\s*\("""),
        "Test waits on wall-clock time",
        "Sleeping makes the test slow and flaky: it fails on a loaded CI runner and passes for the wrong reason elsewhere.",
        "Wait on the condition (polling with a deadline) or inject a fake clock.",
        include_tests=True,
    ),
)

_SPECIFIC_SECRETS = {
    "private-key", "aws-access-key", "github-token", "openai-key", "slack-token", "stripe-key",
    "google-api-key", "connection-string",
}  # fmt: skip
_DEF_PY = re.compile(r"^(\s*)(async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
_CLASS_PY = re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)")
_DEF_OTHER = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)|^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("
)


def _applies(rule: LineRule, path: str, tests_only: bool = False) -> bool:
    if rule.suffixes and not path.lower().endswith(rule.suffixes):
        return False
    if tests_only:
        return is_test_path(path)
    if is_doc_path(path):
        return False
    return rule.include_tests or not is_test_path(path)


def _line_rules(agent: AgentType, rules: tuple[LineRule, ...], files: list[DiffFile], tests_only: bool = False) -> list[Finding]:
    out: list[Finding] = []
    for f in files:
        parsed = parse_patch(f.patch)
        for line_no, text in sorted(parsed.added.items()):
            if len(text) > 2000:  # minified or generated
                continue
            for rule in rules:
                if _applies(rule, f.path, tests_only) and rule.pattern.search(text):
                    out.append(
                        Finding.build(
                            agent=agent, severity=rule.severity, category=rule.category, summary=rule.summary,
                            file_path=f.path, line_start=line_no, confidence=rule.confidence,
                            rationale=rule.rationale, suggestion=rule.suggestion, evidence=text.strip()[:240],
                            source="rules",
                        )  # fmt: skip
                    )
                    break  # one finding per line per pack: the first (most specific) rule wins
    return out


# ------------------------------------------------------------------ security
def security_rules(files: list[DiffFile], pr_text: str = "") -> list[Finding]:
    findings = _line_rules(AgentType.SECURITY, SECURITY_RULES, files)
    for f in files:
        parsed = parse_patch(f.patch)
        in_test = is_test_path(f.path)
        for line_no, text in sorted(parsed.added.items()):
            for hit in find_secrets(text):
                specific = hit.kind in _SPECIFIC_SECRETS
                if in_test and not specific:
                    continue
                findings.append(
                    Finding.build(
                        agent=AgentType.SECURITY,
                        severity=Severity.CRITICAL if specific else Severity.HIGH,
                        category="hardcoded-secret",
                        summary=f"Credential committed to the repository ({hit.kind})",
                        file_path=f.path, line_start=line_no,
                        confidence=0.92 if specific else 0.72,
                        rationale="Anything committed is readable by everyone with repository access and stays in git history after it is deleted. Treat this credential as leaked.",
                        suggestion="Revoke and rotate it now, load it from the environment or a secret manager, and remove it from history.",
                        evidence=f"{hit.kind}: {hit.preview}",
                        source="guard" if specific else "rules",
                    )  # fmt: skip
                )
        added_text = "\n".join(text for _, text in sorted(parsed.added.items()))
        line_numbers = sorted(parsed.added)
        for signal in scan_untrusted(added_text):
            line_no = line_numbers[min(signal.line - 1, len(line_numbers) - 1)] if line_numbers else 1
            findings.append(_injection_finding(f.path, line_no, signal.kind, signal.excerpt))
    for signal in scan_untrusted(pr_text):
        target = files[0].path if files else "PULL_REQUEST_DESCRIPTION"
        findings.append(_injection_finding(target, 1, signal.kind, signal.excerpt, where="the pull request description"))
    return findings


def _injection_finding(path: str, line: int, kind: str, excerpt: str, where: str = "this change") -> Finding:
    return Finding.build(
        agent=AgentType.SECURITY, severity=Severity.HIGH, category="prompt-injection",
        summary=f"Text in {where} is written as instructions to an automated reviewer ({kind})",
        file_path=path, line_start=line, confidence=0.9,
        rationale="Content addressed to AI reviewers is an attempt to steer automated review. It was not followed. A human should read this change in full.",
        suggestion="Remove the text, or explain why it is needed.",
        evidence=excerpt, source="guard",
    )  # fmt: skip


# ------------------------------------------------------------------- quality
def quality_rules(files: list[DiffFile], pr_text: str = "") -> list[Finding]:
    findings = _line_rules(AgentType.QUALITY, QUALITY_RULES, files)
    for f in files:
        if not f.path.endswith(PY) or is_test_path(f.path):
            continue
        parsed = parse_patch(f.patch)
        for line_no, text in sorted(parsed.added.items()):
            # except ...:\n    pass  (split across two lines)
            if re.match(r"^\s*except\b.*:\s*$", text) and (parsed.text_at(line_no + 1) or "").strip() in ("pass", "..."):
                if not re.match(r"^\s*except\s*:\s*$", text):  # the bare form is already a line rule
                    findings.append(
                        Finding.build(
                            agent=AgentType.QUALITY, severity=Severity.MEDIUM, category="swallowed-exception",
                            summary="Exception is caught and silently ignored", file_path=f.path,
                            line_start=line_no, line_end=line_no + 1, confidence=0.72,
                            rationale="The handler does nothing, so a failure here continues as if it had succeeded and leaves no trace.",
                            suggestion="Log the exception with context, or let it propagate.",
                            evidence=text.strip(), source="rules",
                        )  # fmt: skip
                    )
            if re.search(r"\btime\.sleep\s*\(|\brequests\.(?:get|post|put|delete|request)\s*\(", text):
                enclosing = _enclosing_def(parsed, line_no)
                if enclosing and enclosing[1]:
                    findings.append(
                        Finding.build(
                            agent=AgentType.QUALITY, severity=Severity.MEDIUM, category="blocking-call-in-async",
                            summary=f"Blocking call inside async function `{enclosing[0]}`", file_path=f.path,
                            line_start=line_no, confidence=0.8,
                            rationale="A synchronous call blocks the event loop, so every other coroutine on this worker stalls until it returns.",
                            suggestion="Use the async equivalent (asyncio.sleep, an async HTTP client) or run it in a thread with asyncio.to_thread.",
                            evidence=text.strip()[:240], source="rules",
                        )  # fmt: skip
                    )
    return findings


def _enclosing_def(parsed: ParsedPatch, line_no: int) -> tuple[str, bool] | None:
    """Nearest visible ``def`` above ``line_no`` with smaller indentation -> (name, is_async)."""
    target = parsed.text_at(line_no) or ""
    indent = len(target) - len(target.lstrip())
    for candidate in range(line_no - 1, max(0, line_no - 80), -1):
        text = parsed.text_at(candidate)
        if text is None:
            return None  # ran off the visible hunk: do not guess
        m = _DEF_PY.match(text)
        if m and len(m.group(1)) < indent:
            return m.group(3), bool(m.group(2))
    return None


# --------------------------------------------------------------------- tests
def tests_rules(files: list[DiffFile], pr_text: str = "") -> list[Finding]:
    findings = _line_rules(AgentType.TESTS, TEST_RULES, files, tests_only=True)
    test_files = [f for f in files if is_test_path(f.path) and f.status != "removed"]
    test_text = "\n".join("\n".join(parse_patch(f.patch).added.values()) for f in test_files)

    for f in files:
        if f.status == "removed":
            continue
        parsed = parse_patch(f.patch)
        if is_test_path(f.path):
            findings.extend(_assertion_free_tests(f.path, parsed))
            removed_asserts = sum(1 for _, t in parsed.removed if re.search(r"\bassert\b|\bexpect\s*\(", t))
            added_asserts = sum(1 for t in parsed.added.values() if re.search(r"\bassert\b|\bexpect\s*\(", t))
            if removed_asserts - added_asserts >= 2 and parsed.commentable:
                findings.append(
                    Finding.build(
                        agent=AgentType.TESTS, severity=Severity.MEDIUM, category="removed-assertions",
                        summary=f"{removed_asserts - added_asserts} more assertions removed than added in this test file",
                        file_path=f.path, line_start=min(parsed.commentable), confidence=0.6,
                        rationale="Net removal of assertions usually means the test was weakened to make a change pass.",
                        suggestion="Confirm the removed checks are obsolete, or restore them.",
                        evidence=next(t.strip() for _, t in parsed.removed if re.search(r"\bassert\b|\bexpect\s*\(", t))[:240],
                        source="rules",
                    )  # fmt: skip
                )
            continue
        if not is_source_path(f.path):
            continue
        new_functions = _new_public_functions(f.path, parsed)
        if not new_functions:
            continue
        names = [name for _, name in new_functions]
        if not test_files:
            line_no, _ = new_functions[0]
            findings.append(
                Finding.build(
                    agent=AgentType.TESTS, severity=Severity.MEDIUM, category="missing-tests",
                    summary=f"New behaviour ships without tests: {_listed(names)}",
                    file_path=f.path, line_start=line_no,
                    confidence=min(0.75, 0.5 + 0.05 * len(names)),
                    rationale="This pull request adds functions but changes no test file, so nothing would fail if they were wrong.",
                    suggestion=f"Add tests covering the normal path and at least one failure path of {_listed(names[:2])}.",
                    evidence=(parsed.text_at(line_no) or "").strip()[:240], source="rules",
                )  # fmt: skip
            )
        else:
            untested = [(ln, n) for ln, n in new_functions if not re.search(rf"\b{re.escape(n)}\b", test_text)]
            if untested:
                line_no, _ = untested[0]
                findings.append(
                    Finding.build(
                        agent=AgentType.TESTS, severity=Severity.LOW, category="missing-tests",
                        summary=f"New functions are not referenced by the tests in this change: {_listed([n for _, n in untested])}",
                        file_path=f.path, line_start=line_no, confidence=0.5,
                        rationale="The tests touched by this pull request never mention these functions.",
                        suggestion="Add a direct test, or point to the existing test that covers them.",
                        evidence=(parsed.text_at(line_no) or "").strip()[:240], source="rules",
                    )  # fmt: skip
                )
    return findings


def _new_public_functions(path: str, parsed: ParsedPatch) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for line_no, text in sorted(parsed.added.items()):
        if path.endswith(PY):
            m = _DEF_PY.match(text)
            name = m.group(3) if m else None
        else:
            m2 = _DEF_OTHER.match(text)
            name = (m2.group(1) or m2.group(2)) if m2 else None
        if name and not name.startswith("_") and name not in ("main", "constructor"):
            out.append((line_no, name))
    return out


def _assertion_free_tests(path: str, parsed: ParsedPatch) -> list[Finding]:
    findings: list[Finding] = []
    lines = sorted(parsed.added.items())
    for i, (line_no, text) in enumerate(lines):
        m = re.match(r"^(\s*)(?:async\s+)?def\s+(test_\w+)\s*\(", text)
        if not m:
            continue
        indent = len(m.group(1))
        body: list[str] = []
        expected = line_no + 1
        complete = True
        for next_no, next_text in lines[i + 1 :]:
            if next_no != expected:  # body continues outside the added block: cannot judge
                complete = parsed.text_at(expected) is None
                break
            if next_text.strip() and len(next_text) - len(next_text.lstrip()) <= indent:
                break
            body.append(next_text)
            expected += 1
        if not complete or not any(b.strip() for b in body):
            continue
        if not re.search(r"\bassert|\braises\b|\bexpect|\.assert_|\bfail\(|\bverify", "\n".join(body)):
            findings.append(
                Finding.build(
                    agent=AgentType.TESTS, severity=Severity.MEDIUM, category="assertion-free-test",
                    summary=f"`{m.group(2)}` has no assertion", file_path=path, line_start=line_no,
                    confidence=0.7,
                    rationale="A test that asserts nothing passes as long as the code does not raise, so it would not catch a wrong result.",
                    suggestion="Assert on the returned value or the observable side effect.",
                    evidence=text.strip()[:240], source="rules",
                )  # fmt: skip
            )
    return findings


# ---------------------------------------------------------------------- docs
_FLAG = re.compile(r"""add_argument\(\s*["'](--[\w-]+)|os\.(?:environ\.get|getenv)\(\s*["'](\w+)["']|os\.environ\[\s*["'](\w+)["']|process\.env\.([A-Z][A-Z0-9_]+)""")


def docs_rules(files: list[DiffFile], pr_text: str = "") -> list[Finding]:
    findings: list[Finding] = []
    docs_touched = any(is_doc_path(f.path) or f.path.endswith(".env.example") for f in files)
    for f in files:
        if f.status == "removed" or not is_source_path(f.path):
            continue
        parsed = parse_patch(f.patch)
        if f.path.endswith(PY):
            undocumented = _undocumented_python(parsed)
            if undocumented:
                line_no = undocumented[0][0]
                names = [n for _, n in undocumented]
                findings.append(
                    Finding.build(
                        agent=AgentType.DOCS, severity=Severity.LOW, category="missing-docstring",
                        summary=f"New public API without a docstring: {_listed(names)}",
                        file_path=f.path, line_start=line_no, confidence=0.6,
                        rationale="Public functions and classes are read far more often than they are written; without a docstring the next reader has to reverse-engineer intent, parameters and failure modes.",
                        suggestion="Add a docstring stating what it does, its parameters, and what it returns or raises.",
                        evidence=(parsed.text_at(line_no) or "").strip()[:240], source="rules",
                    )  # fmt: skip
                )
        if not docs_touched:
            options: list[tuple[int, str]] = []
            for line_no, text in sorted(parsed.added.items()):
                for m in _FLAG.finditer(text):
                    options.append((line_no, next(g for g in m.groups() if g)))
            if options:
                findings.append(
                    Finding.build(
                        agent=AgentType.DOCS, severity=Severity.LOW, category="undocumented-configuration",
                        summary=f"New configuration is not documented: {_listed(sorted({n for _, n in options}))}",
                        file_path=f.path, line_start=options[0][0], confidence=0.6,
                        rationale="This change reads new flags or environment variables, but no README, docs page or .env.example was updated, so operators cannot discover them.",
                        suggestion="Document each option (purpose, default, example) where the others are documented.",
                        evidence=(parsed.text_at(options[0][0]) or "").strip()[:240], source="rules",
                    )  # fmt: skip
                )
    return findings


def _undocumented_python(parsed: ParsedPatch) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for line_no, text in sorted(parsed.added.items()):
        m = _DEF_PY.match(text)
        name = m.group(3) if m else None
        if not name:
            c = _CLASS_PY.match(text)
            name = c.group(2) if c else None
        if not name or name.startswith("_") or name.startswith("test"):
            continue
        # Walk to the end of the signature, then look at the first body line.
        cursor = line_no
        while cursor - line_no < 12 and not (parsed.text_at(cursor) or "").rstrip().endswith(":"):
            cursor += 1
        body = parsed.text_at(cursor + 1)
        if body is None:
            continue  # the body is outside the visible hunk: do not guess
        if not re.match(r"""^\s*[rRbBuU]{0,2}(?:\"\"\"|''')""", body):
            out.append((line_no, name))
    return out


def _listed(names: list[str], limit: int = 4) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


RULE_PACKS = {
    AgentType.SECURITY: security_rules,
    AgentType.QUALITY: quality_rules,
    AgentType.TESTS: tests_rules,
    AgentType.DOCS: docs_rules,
}


def run_rules(agent: AgentType, files: list[DiffFile], pr_text: str = "") -> list[Finding]:
    seen: set[str] = set()
    out: list[Finding] = []
    for finding in RULE_PACKS[agent](files, pr_text):
        if finding.id not in seen:
            seen.add(finding.id)
            out.append(finding)
    return out
