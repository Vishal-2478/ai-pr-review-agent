"""Turn findings into one structured GitHub review.

The output of the whole system is a single review: a short summary body plus
inline comments attached to exact lines. Each comment states where the
finding came from, how severe it is, why the agent believes it and how sure
it is, and tells the author how to dispute it.
"""

from __future__ import annotations

from typing import Any

from backend.security.secret_masking import mask

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEV_LABEL = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW", "info": "INFO"}


def marker(review_id: str) -> str:
    """Hidden tag that lets us recognise our own review and never post it twice."""
    return f"<!-- pr-review-agent:review={review_id} -->"


def _footer(f: dict[str, Any]) -> str:
    parts = [f"`{f['finding_id']}`", f"confidence {f['confidence']:.0%}"]
    if f.get("corroborated_by"):
        parts.append("also flagged by " + ", ".join(f["corroborated_by"]))
    if f.get("source") in ("rules", "guard"):
        parts.append("deterministic check")
    parts.append(f"wrong? reply `/dispute {f['finding_id']} <reason>`")
    return "<sub>" + " · ".join(parts) + "</sub>"


def comment_body(f: dict[str, Any]) -> str:
    lines = [f"**{_SEV_LABEL.get(f['severity'], f['severity'].upper())} · {f['agent_type']} · {f['category']}**", "", mask(f["summary"])]
    if f.get("rationale"):
        lines += ["", f"**Why:** {mask(f['rationale'])}"]
    if f.get("suggestion"):
        lines += ["", f"**Suggested fix:** {mask(f['suggestion'])}"]
    lines += ["", _footer(f)]
    return "\n".join(lines)


def format_review(
    review_id: str,
    head_sha: str,
    findings: list[dict[str, Any]],
    *,
    overall_confidence: float | None,
    coverage: float | None,
    max_inline: int = 12,
    suppressed: int = 0,
    approved_by: str = "",
    inline: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    ordered = sorted(findings, key=lambda f: (_SEV_ORDER.get(f["severity"], 9), -f["confidence"], f["file_path"], f["line_start"]))
    inline_findings = [f for f in ordered if inline and f.get("in_diff")][:max_inline]
    inline_ids = {f["finding_id"] for f in inline_findings}
    in_body = [f for f in ordered if f["finding_id"] not in inline_ids]

    counts: dict[str, int] = {}
    for f in ordered:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    tally = ", ".join(f"{n} {sev}" for sev, n in sorted(counts.items(), key=lambda kv: _SEV_ORDER.get(kv[0], 9)))

    body = ["## PR Review Agent", ""]
    if ordered:
        body.append(
            f"Reviewed `{head_sha[:7]}` from four angles (security, quality, tests, docs). "
            f"**{len(ordered)} finding{'s' if len(ordered) != 1 else ''}** worth your attention: {tally}."
        )
        body += ["", "| Severity | Where | Finding |", "| --- | --- | --- |"]
        for f in ordered:
            where = f"`{f['file_path']}:{f['line_start']}`"
            body.append(f"| {_SEV_LABEL.get(f['severity'], f['severity'])} | {where} | {mask(f['summary'])} (`{f['finding_id']}`) |")
    else:
        body.append(f"Reviewed `{head_sha[:7]}` from four angles (security, quality, tests, docs). Nothing met the bar for your attention.")

    if in_body:
        body += ["", "### Details" if not inline_findings else "### Findings not attached to a changed line"]
        for f in in_body:
            body += ["", f"#### `{f['file_path']}:{f['line_start']}`", "", comment_body(f)]

    notes = []
    if overall_confidence is not None:
        notes.append(f"overall confidence {overall_confidence:.0%}")
    if coverage is not None and coverage < 1.0:
        notes.append(f"coverage {coverage:.0%} (a specialist ran degraded)")
    if approved_by:
        notes.append(f"approved by {approved_by}")
    if suppressed:
        notes.append(f"{suppressed} lower-value finding{'s' if suppressed != 1 else ''} suppressed")
    notes.append("advisory only: this review never approves or blocks a merge")
    body += ["", "<sub>" + " · ".join(notes) + "</sub>", marker(review_id)]

    comments = [
        {"path": f["file_path"], "line": int(f["line_start"]), "side": "RIGHT", "body": comment_body(f)}
        for f in inline_findings
    ]
    return "\n".join(body), comments
