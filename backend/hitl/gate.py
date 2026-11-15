"""The selectivity gate.

L0 of the design: the system optimises for surfacing findings worth a senior
engineer's attention and deferring the rest, not for maximal output. This
function is where that sentence becomes code. For every finding it answers
"is this worth anyone's attention?", and for the review as a whole it answers
"can the agent act on its own, or does a human decide?".

    finding below the noise floor                 -> suppressed (recorded, never shown)
    low/info finding below the threshold          -> suppressed (not worth attention)
    any CRITICAL finding                          -> escalate: page a human, post nothing
    medium+ finding the agent is unsure about     -> human approval queue
    overall confidence below the threshold        -> human approval queue
    untrusted input tried to steer the review     -> human approval queue
    otherwise                                     -> post automatically

It is a pure function of its inputs, so the whole decision table is unit
tested and every decision can be replayed from the event log.
"""

from __future__ import annotations

from backend.agents.contracts import Finding, GateOutcome
from backend.config import Settings
from backend.models.enums import FindingStatus, GateDecision, Severity


def apply_gate(
    findings: list[Finding],
    coverage: float,
    settings: Settings,
    *,
    injection_detected: bool = False,
) -> GateOutcome:
    threshold = settings.hitl_confidence_threshold
    floor = settings.noise_floor
    reasons: list[str] = []
    surviving: list[Finding] = []
    suppressed: list[Finding] = []

    for f in findings:
        if f.confidence < floor:
            f.status = FindingStatus.SUPPRESSED
            f.adjustments.append(f"suppressed: confidence {f.confidence:.2f} below noise floor {floor:.2f}")
            suppressed.append(f)
        elif f.severity in (Severity.LOW, Severity.INFO) and f.confidence < threshold:
            f.status = FindingStatus.SUPPRESSED
            f.adjustments.append(f"suppressed: {f.severity.value} finding below threshold {threshold:.2f}")
            suppressed.append(f)
        else:
            surviving.append(f)

    if surviving:
        weight = sum(f.severity.weight for f in surviving)
        overall = sum(f.severity.weight * f.confidence for f in surviving) / weight * coverage
    else:
        overall = coverage
    overall = round(overall, 4)

    criticals = [f for f in surviving if f.severity is Severity.CRITICAL]
    unsure = [f for f in surviving if f.severity.rank >= Severity.MEDIUM.rank and f.confidence < threshold]

    if settings.autonomy_level == "shadow":
        decision = GateDecision.SHADOW
        reasons.append("shadow mode: findings are recorded and never posted")
    elif criticals:
        decision = GateDecision.ESCALATE
        reasons.append(f"{len(criticals)} critical finding(s): a human decides, regardless of confidence")
    elif not surviving:
        decision = GateDecision.NO_FINDINGS
        reasons.append("nothing met the bar for attention")
    elif settings.autonomy_level == "human_approves_all":
        decision = GateDecision.HUMAN_REVIEW
        reasons.append("autonomy level requires human approval for every review")
    elif injection_detected:
        decision = GateDecision.HUMAN_REVIEW
        reasons.append("the pull request contains text addressed to automated reviewers")
    elif unsure:
        decision = GateDecision.HUMAN_REVIEW
        reasons.append(f"{len(unsure)} medium-or-higher finding(s) below confidence {threshold:.2f}")
    elif overall < threshold:
        decision = GateDecision.HUMAN_REVIEW
        reasons.append(f"overall confidence {overall:.2f} below {threshold:.2f} (coverage {coverage:.0%})")
    else:
        decision = GateDecision.AUTO_POST
        reasons.append(f"overall confidence {overall:.2f} with no critical findings")

    if coverage < 1.0:
        reasons.append(f"coverage {coverage:.0%}: not every specialist completed normally")
    if suppressed:
        reasons.append(f"{len(suppressed)} low-value finding(s) suppressed")

    needs_human = decision in (GateDecision.HUMAN_REVIEW, GateDecision.ESCALATE)
    for f in surviving:
        f.status = FindingStatus.PENDING_HUMAN if needs_human else FindingStatus.PROPOSED

    top = max((f.severity.rank for f in surviving), default=0)
    return GateOutcome(
        decision=decision,
        overall_confidence=overall,
        coverage=coverage,
        reasons=reasons,
        to_post=[f.id for f in surviving] if decision is GateDecision.AUTO_POST else [],
        to_review=[f.id for f in surviving] if needs_human else [],
        suppressed=[f.id for f in suppressed],
        # Queue order: severity first, then how much there is to look at.
        priority=top * 100 + min(len(surviving), 50),
    )
