"""validate/citations.py — a Decision may only cite ids that are in its EvidenceSet.

CONTRACT Section 2: every spending_changes event_id must be in
evidence.event_ids; every option_id used must be in evidence.option_ids;
not_affordable rows must have no changes; and any `event_<n>` id that appears in
the free text (explanation or notes) must also be in evidence.
"""
from __future__ import annotations

import re

from buyorwait.types import Decision, EvidenceSet

EVENT_MENTION_RE = re.compile(r"\bevent_\d+\b")


def validate_citations(decision: Decision, evidence: EvidenceSet) -> list[str]:
    """Return a list of citation problems; [] when every cited id is supported."""
    problems: list[str] = []

    if evidence is None:
        return ["citations: no EvidenceSet supplied"]

    if evidence.request_id != decision.request_id:
        problems.append(
            f"evidence: built for {evidence.request_id!r}, decision is for "
            f"{decision.request_id!r}"
        )

    event_ids = set(evidence.event_ids or ())
    option_ids = set(evidence.option_ids or ())

    changes = list(decision.spending_changes_needed or ())

    # not_affordable rows must not carry spending changes.
    if decision.affordability_status == "not_affordable" and changes:
        problems.append(
            "spending_changes_needed: not_affordable decisions must not request changes"
        )

    for change in changes:
        if change.event_id not in event_ids:
            problems.append(
                f"spending_changes_needed: event_id {change.event_id!r} is not in the evidence"
            )

    plan = decision.chosen_plan
    option_id = getattr(plan, "option_id", None) if plan is not None else None
    if option_id is not None and option_id not in option_ids:
        problems.append(
            f"payment_plan: option_id {option_id!r} is not in the evidence"
        )

    # Free text must not name an event the decision never looked at.
    texts = [decision.decision_explanation or ""] + list(decision.notes or ())
    for text in texts:
        for mention in EVENT_MENTION_RE.findall(text):
            if mention not in event_ids:
                problems.append(
                    f"explanation/notes: mentions {mention!r} which is not in the evidence"
                )

    # Stable, de-duplicated output.
    seen: set[str] = set()
    unique: list[str] = []
    for p in problems:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique
