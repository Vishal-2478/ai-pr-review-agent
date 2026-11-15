"""The context graph, enforced: architectural invariants that fail the build when broken."""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _modules() -> list[Path]:
    return [p for p in BACKEND.rglob("*.py") if "__pycache__" not in p.parts]


def test_only_the_orchestrator_knows_langgraph():
    """ADR-001: LangGraph sits behind WorkflowEngine. Swapping it must touch one package."""
    allowed = {BACKEND / "orchestrator" / name for name in ("graph.py", "langgraph_engine.py", "redis_checkpointer.py")}
    allowed.add(BACKEND / "container.py")  # composition root: picks the checkpointer
    offenders = [
        str(p.relative_to(BACKEND))
        for p in _modules()
        if p not in allowed and any(m.split(".")[0] in ("langgraph", "langchain_core") for m in _imports(p))
    ]
    assert offenders == []


def test_core_and_models_depend_on_nothing_else():
    """ADR-002: dependencies point inward. ``core`` and ``models`` are the centre."""
    for package, may_import in (("core", {"backend.core"}), ("models", {"backend.models", "backend.core"})):
        for path in (BACKEND / package).rglob("*.py"):
            internal = {m for m in _imports(path) if m.startswith("backend")}
            bad = {m for m in internal if not any(m == ok or m.startswith(ok + ".") for ok in may_import)}
            assert not bad, f"{path.relative_to(BACKEND)} imports {sorted(bad)}"


def test_specialists_never_talk_to_a_vendor_sdk():
    """Agents see LLMClient and the tool registry, never openai/anthropic/httpx directly."""
    for path in (BACKEND / "agents").rglob("*.py"):
        vendors = {m.split(".")[0] for m in _imports(path)} & {"openai", "anthropic", "httpx", "redis", "asyncpg"}
        assert not vendors, f"{path.name} imports {sorted(vendors)}"


def test_the_gate_is_deterministic_code():
    """Routing is never delegated to a model: the gate imports no LLM client."""
    for name in ("gate.py",):
        imports = _imports(BACKEND / "hitl" / name)
        assert not any("llm" in m or "openai" in m or "anthropic" in m for m in imports)
    assert not any("llm" in m for m in _imports(BACKEND / "agents" / "aggregator.py"))


def test_ingress_does_no_review_work():
    """I2: the webhook route may enqueue; it may not import the pipeline it feeds."""
    imports = _imports(BACKEND / "webhook_receiver" / "router.py")
    forbidden = [m for m in imports if m.startswith(("backend.agents", "backend.orchestrator", "backend.memory", "backend.tools"))]
    assert forbidden == []


def test_events_table_is_append_only_in_the_schema():
    sql = (BACKEND.parent / "scripts" / "migrations" / "035_events_append_only.sql").read_text().lower()
    assert "before update or delete" in sql and "truncate" in sql


def test_every_prompt_is_a_versioned_file():
    templates = sorted(p.name for p in (BACKEND / "prompts" / "templates").glob("*.md"))
    assert templates, "prompts live in files, not in code"
    assert all(".v" in name for name in templates)
    for agent in ("security", "quality", "tests", "docs", "base"):
        assert any(name.startswith(agent + ".v") for name in templates)
