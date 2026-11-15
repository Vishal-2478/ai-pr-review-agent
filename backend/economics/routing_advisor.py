"""Model-routing advice from observed cost, latency and human feedback.

Advice, not automation: changing which model reviews security code is a
decision for a person. The advisor turns the rollups into concrete,
explainable suggestions and the dashboard shows them.
"""

from __future__ import annotations

from typing import Any


def advise(
    agent_health: list[dict[str, Any]],
    *,
    agent_timeout_s: float,
    min_evidence: int = 5,
) -> list[dict[str, str]]:
    advice: list[dict[str, str]] = []
    specialists = [a for a in agent_health if a["agent"] in ("security", "quality", "tests", "docs")]
    total_cost = sum(a["cost_usd"] for a in specialists) or 0.0
    for a in specialists:
        share = a["cost_usd"] / total_cost if total_cost else 0.0
        feedback = a.get("feedback_total") or 0
        negative = (a.get("feedback_negative") or 0) / feedback if feedback else 0.0
        if feedback >= min_evidence and negative >= 0.4:
            advice.append({
                "agent": a["agent"], "kind": "quality",
                "message": f"{negative:.0%} of {feedback} human verdicts were negative. Tighten the prompt or "
                           "route this agent to a stronger model before trusting its confidence.",
            })  # fmt: skip
        elif feedback >= min_evidence and negative <= 0.1 and share >= 0.35:
            advice.append({
                "agent": a["agent"], "kind": "cost",
                "message": f"{share:.0%} of spend with only {negative:.0%} negative verdicts over {feedback} "
                           "findings. Try a cheaper model and compare on the golden set (make eval).",
            })  # fmt: skip
        p95 = a.get("p95_latency_ms")
        if p95 and p95 >= agent_timeout_s * 1000 * 0.8:
            advice.append({
                "agent": a["agent"], "kind": "latency",
                "message": f"p95 latency {p95 / 1000:.1f}s is within 20% of the {agent_timeout_s:.0f}s node "
                           "timeout. Reduce retrieved context or switch model before timeouts cut coverage.",
            })  # fmt: skip
        if (a.get("errors") or 0) >= 5 and (a.get("errors") or 0) >= (a.get("llm_calls") or 0):
            advice.append({
                "agent": a["agent"], "kind": "reliability",
                "message": f"{a['errors']} errors against {a.get('llm_calls') or 0} model calls. Check provider "
                           "health and the circuit-breaker state.",
            })  # fmt: skip
    return advice
