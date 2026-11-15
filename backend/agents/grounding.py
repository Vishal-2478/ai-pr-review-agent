"""Fact-check layer: does the finding point at something that exists?

A model can state something false with complete confidence. Before a finding
leaves a specialist it is checked against the only ground truth available,
the diff and the retrieved code:

- the cited file must be part of the change (or of the retrieved context);
- the quoted evidence must actually appear there;
- the cited line is snapped to where the evidence really is.

A finding that fails is not thrown away silently. Its confidence is cut and
the reason is recorded, and the selectivity gate decides what happens next.
"""

from __future__ import annotations

import re

from backend.agents.contracts import Finding
from backend.integrations.diff import ParsedPatch, parse_patch
from backend.models.schemas import DiffFile, RetrievedContext
from backend.security.secret_masking import mask

UNGROUNDED_FACTOR = 0.55
OUTSIDE_CHANGE_FACTOR = 0.4
NO_EVIDENCE_FACTOR = 0.8


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _strip_prefix(line: str) -> str:
    """Models sometimes copy the rendered prefix ("+    42 | code") into the quote."""
    return re.sub(r"^\s*[+\-]?\s*\d*\s*\|\s?", "", line)


def _resolve_path(path: str, known: list[str]) -> str | None:
    cleaned = re.sub(r"^(?:[ab]/|\./)", "", path.strip())
    if cleaned in known:
        return cleaned
    suffix = [k for k in known if k.endswith("/" + cleaned) or cleaned.endswith("/" + k)]
    return suffix[0] if len(suffix) == 1 else None


def _locate(evidence: str, parsed: ParsedPatch) -> int | None:
    """New-file line where the first meaningful line of the quote appears."""
    for raw in evidence.splitlines():
        needle = _norm(_strip_prefix(raw))
        if len(needle) < 6:
            continue
        for line_no in sorted(parsed.commentable):
            if needle in _norm(parsed.text_at(line_no) or ""):
                return line_no
        return None  # the first meaningful line decides
    return None


def ground_findings(findings: list[Finding], files: list[DiffFile], context: RetrievedContext) -> list[Finding]:
    by_path = {f.path: f for f in files}
    parsed_cache: dict[str, ParsedPatch] = {}
    context_by_path: dict[str, str] = {}
    for chunk in context.chunks:
        context_by_path[chunk.path] = context_by_path.get(chunk.path, "") + "\n" + chunk.content

    for finding in findings:
        finding.evidence = mask(finding.evidence)
        resolved = _resolve_path(finding.file_path, list(by_path))
        if resolved is None:
            ctx_path = _resolve_path(finding.file_path, list(context_by_path))
            if ctx_path and finding.evidence and _norm(_strip_prefix(finding.evidence.splitlines()[0])) in _norm(context_by_path[ctx_path]):
                finding.file_path = ctx_path
                finding.grounded = True  # real code, but outside the diff: cannot be an inline comment
                finding.adjust(0.85, "cites retrieved code outside the diff")
            else:
                finding.adjust(OUTSIDE_CHANGE_FACTOR, "cites a file that is not part of this change")
            continue

        finding.file_path = resolved
        parsed = parsed_cache.setdefault(resolved, parse_patch(by_path[resolved].patch))

        if finding.source in ("rules", "guard"):
            # Rule hits are produced from a specific added line and quote it.
            finding.grounded = True
            finding.in_diff = finding.line_start in parsed.commentable
            continue

        if finding.evidence:
            located = _locate(finding.evidence, parsed)
            if located is not None:
                span = finding.line_end - finding.line_start
                if abs(located - finding.line_start) > 2:
                    finding.adjustments.append(f"line corrected {finding.line_start} -> {located} from quoted evidence")
                    finding.line_start, finding.line_end = located, located + max(0, min(span, 20))
                finding.grounded = True
            else:
                removed = _norm(" ".join(text for _, text in parsed.removed))
                first = _norm(_strip_prefix(finding.evidence.splitlines()[0]))
                if len(first) >= 6 and first in removed:
                    finding.adjust(0.7, "quoted code was removed by this change")
                else:
                    finding.adjust(UNGROUNDED_FACTOR, "quoted evidence not found in the diff")
        else:
            finding.adjust(NO_EVIDENCE_FACTOR, "no evidence quoted")

        finding.in_diff = finding.line_start in parsed.commentable
        if not finding.in_diff and finding.grounded is False:
            finding.adjust(0.8, "cited line is not part of the diff")
    return findings
