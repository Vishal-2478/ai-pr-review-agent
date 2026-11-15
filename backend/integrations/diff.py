"""Unified-diff parsing.

Two consumers need exact line numbers: the prompt (so a model can cite
``line 42`` and be right) and the GitHub review API (which rejects an inline
comment on a line that is not part of the diff).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.models.schemas import DiffFile

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ParsedPatch:
    added: dict[int, str] = field(default_factory=dict)  # new-file line -> text
    context: dict[int, str] = field(default_factory=dict)  # new-file line -> text
    removed: list[tuple[int, str]] = field(default_factory=list)  # old-file line, text

    @property
    def commentable(self) -> set[int]:
        """Lines on the RIGHT side of the diff that GitHub accepts comments on."""
        return set(self.added) | set(self.context)

    def text_at(self, line: int) -> str | None:
        return self.added.get(line, self.context.get(line))


def parse_patch(patch: str) -> ParsedPatch:
    parsed = ParsedPatch()
    old_line = new_line = 0
    in_hunk = False
    for raw in (patch or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            old_line, new_line = int(m.group(1)), int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            parsed.added[new_line] = raw[1:]
            new_line += 1
        elif raw.startswith("-"):
            parsed.removed.append((old_line, raw[1:]))
            old_line += 1
        else:
            parsed.context[new_line] = raw[1:] if raw.startswith(" ") else raw
            old_line += 1
            new_line += 1
    return parsed


def parse_unified_diff(diff_text: str) -> list[DiffFile]:
    """Split a full ``git diff`` into per-file patches (used by the CLI, demo and evals)."""
    files: list[DiffFile] = []
    current: DiffFile | None = None
    patch_lines: list[str] = []

    def flush() -> None:
        nonlocal current, patch_lines
        if current is not None:
            current.patch = "\n".join(patch_lines)
            parsed = parse_patch(current.patch)
            current.additions = len(parsed.added)
            current.deletions = len(parsed.removed)
            files.append(current)
        current, patch_lines = None, []

    old_path = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            path = m.group(2) if m else line.split(" b/")[-1]
            current = DiffFile(path=path)
            old_path = m.group(1) if m else path
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "removed"
        elif line.startswith("rename from "):
            current.status = "renamed"
            current.previous_path = line[len("rename from ") :]
        elif line.startswith("--- "):
            if line[4:].strip() == "/dev/null":
                current.status = "added"
        elif line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current.status = "removed"
                current.path = old_path
            elif target.startswith("b/"):
                current.path = target[2:]
        elif line.startswith("@@") or patch_lines:
            patch_lines.append(line)
    flush()
    return files


def render_file(file: DiffFile) -> str:
    """Render one file's patch with explicit new-file line numbers."""
    header = f"### {file.path} ({file.status}, +{file.additions} -{file.deletions})"
    if not file.patch:
        return f"{header}\n(no textual patch available: binary or too large)"
    out = [header]
    old_line = new_line = 0
    for raw in file.patch.splitlines():
        m = _HUNK.match(raw)
        if m:
            old_line, new_line = int(m.group(1)), int(m.group(3))
            out.append(raw)
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            out.append(f"+ {new_line:>5} | {raw[1:]}")
            new_line += 1
        elif raw.startswith("-"):
            out.append(f"-       | {raw[1:]}")
            old_line += 1
        else:
            out.append(f"  {new_line:>5} | {raw[1:] if raw.startswith(' ') else raw}")
            old_line += 1
            new_line += 1
    return "\n".join(out)


def render_diff(files: list[DiffFile], max_chars: int) -> tuple[str, list[str]]:
    """Render as many files as fit. Returns the text and the paths left out.

    Dropping whole files (never half a file) keeps the model from reasoning
    about code it can only partly see; the omission is reported so coverage
    can be discounted.
    """
    parts: list[str] = []
    omitted: list[str] = []
    used = 0
    for f in sorted(files, key=_review_priority):
        block = render_file(f)
        if used + len(block) > max_chars and parts:
            omitted.append(f.path)
            continue
        if len(block) > max_chars:
            block = block[:max_chars] + "\n... (file truncated)"
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts), omitted


_LOW_VALUE = (".lock", ".min.js", ".map", ".svg", ".png", ".jpg", ".snap", "package-lock.json", "yarn.lock")


def _review_priority(f: DiffFile) -> tuple[int, int]:
    low = any(f.path.endswith(s) for s in _LOW_VALUE) or "/vendor/" in f.path or "/dist/" in f.path
    return (1 if low else 0, -(f.additions + f.deletions))


def is_test_path(path: str) -> bool:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{p}"
        or "/test/" in f"/{p}"
        or "/__tests__/" in f"/{p}"
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js"))
    )


def is_doc_path(path: str) -> bool:
    p = path.lower()
    return p.endswith((".md", ".rst", ".txt", ".adoc")) or p.startswith("docs/")


SOURCE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs",
    ".c", ".h", ".cpp", ".hpp", ".swift", ".scala", ".sql", ".sh",
)  # fmt: skip


def is_source_path(path: str) -> bool:
    return path.lower().endswith(SOURCE_SUFFIXES) and not is_test_path(path)
