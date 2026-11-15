"""Split source files into retrievable chunks.

Python is chunked on real structure (one chunk per top-level function, and per
method for large classes) using the standard-library AST, so a retrieved
chunk is a complete, named unit. Everything else falls back to overlapping
line windows labelled with the nearest definition above them.
"""

from __future__ import annotations

import ast
import re

from backend.economics.pricing import estimate_tokens
from backend.models.schemas import CodeChunk

MAX_CHUNK_LINES = 120
WINDOW_LINES = 60
WINDOW_OVERLAP = 10
MAX_CHUNK_CHARS = 6000

_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"(?:def|class|function|func|fn|interface|struct|enum|type|trait|impl)\s+([A-Za-z_][\w.]*)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?(?:\(|function)"
)


def chunk_file(repo: str, path: str, content: str) -> list[CodeChunk]:
    if not content.strip():
        return []
    lines = content.splitlines()
    spans: list[tuple[int, int, str]] = []
    if path.endswith(".py"):
        spans = _python_spans(content, len(lines))
    if not spans:
        spans = _window_spans(lines)
    chunks: list[CodeChunk] = []
    for start, end, symbol in spans:
        text = "\n".join(lines[start - 1 : end])[:MAX_CHUNK_CHARS]
        if not text.strip():
            continue
        chunks.append(
            CodeChunk(
                repo=repo, path=path, chunk_index=len(chunks), content=text, symbol=symbol,
                start_line=start, end_line=end, token_count=estimate_tokens(text),
            )  # fmt: skip
        )
    return chunks


def _python_spans(content: str, total_lines: int) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []
    spans: list[tuple[int, int, str]] = []
    cursor = 1

    def start_of(node: ast.AST) -> int:
        decorators = getattr(node, "decorator_list", [])
        return min([node.lineno, *(d.lineno for d in decorators)])  # type: ignore[attr-defined]

    def add(start: int, end: int, symbol: str) -> None:
        # Oversized units are windowed so no single chunk dominates a prompt.
        while end - start + 1 > MAX_CHUNK_LINES:
            spans.append((start, start + MAX_CHUNK_LINES - 1, symbol))
            start += MAX_CHUNK_LINES - WINDOW_OVERLAP
        spans.append((start, end, symbol))

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start, end = start_of(node), node.end_lineno or node.lineno
        if start > cursor:
            add(cursor, start - 1, "<module>")
        if isinstance(node, ast.ClassDef) and end - start + 1 > MAX_CHUNK_LINES:
            inner = start
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    c_start, c_end = start_of(child), child.end_lineno or child.lineno
                    if c_start > inner:
                        add(inner, c_start - 1, node.name)
                    add(c_start, c_end, f"{node.name}.{child.name}")
                    inner = c_end + 1
            if inner <= end:
                add(inner, end, node.name)
        else:
            add(start, end, node.name)
        cursor = end + 1
    if cursor <= total_lines:
        add(cursor, total_lines, "<module>")
    return spans


def _window_spans(lines: list[str]) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    start = 1
    total = len(lines)
    while start <= total:
        end = min(total, start + WINDOW_LINES - 1)
        spans.append((start, end, _nearest_symbol(lines, start, end)))
        if end == total:
            break
        start = end - WINDOW_OVERLAP + 1
    return spans


def _nearest_symbol(lines: list[str], start: int, end: int) -> str:
    for i in range(start - 1, end):
        m = _DEFINITION.match(lines[i])
        if m:
            return m.group(1) or m.group(2) or ""
    for i in range(start - 2, max(-1, start - 200), -1):
        m = _DEFINITION.match(lines[i])
        if m:
            return m.group(1) or m.group(2) or ""
    return ""
