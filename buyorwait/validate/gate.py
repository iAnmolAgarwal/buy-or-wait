"""validate/gate.py — the single fail-closed gate in front of output.csv.

Runs schema, citation and coherence validation. A row that does not pass must be
re-decided or replaced with `output.refusal_row` (CONTRACT Section 5 item 9).
"""
from __future__ import annotations

from typing import Optional

from buyorwait.types import Decision, EvidenceSet, Profile, Request
from buyorwait.validate.citations import validate_citations
from buyorwait.validate.coherence import check_coherence
from buyorwait.validate.schema import validate_row


def gate(
    decision: Decision,
    request: Request,
    profile: Optional[Profile],
    evidence: EvidenceSet,
    row: dict,
) -> tuple[bool, list[str]]:
    """Return (ok, problems). ok is True only when all three validators are clean."""
    problems: list[str] = []
    problems += [f"schema: {p}" for p in validate_row(row, request)]
    problems += [f"citations: {p}" for p in validate_citations(decision, evidence)]
    problems += [f"coherence: {p}" for p in check_coherence(decision, request, profile)]
    return (not problems), problems
