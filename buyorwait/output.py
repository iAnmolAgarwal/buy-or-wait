"""output.py — Decision -> output.csv row, and CSV read/write.

The only place that turns typed objects into the eight OUTPUT_COLUMNS strings.
All number/date rendering goes through buyorwait.formatting.
"""
from __future__ import annotations

import csv
from typing import Optional

from buyorwait.formatting import fmt_date_iso, fmt_plan, fmt_safe_amount
from buyorwait.types import OUTPUT_COLUMNS, Decision, Request

REFUSAL_EXPLANATION = "Insufficient verified data to assess this request safely."
MAX_EXPLANATION = 400


def _clean_explanation(text: str) -> str:
    """One line, trimmed, never longer than the 400-char output budget."""
    flat = " ".join(str(text or "").split())
    if len(flat) > MAX_EXPLANATION:
        flat = flat[:MAX_EXPLANATION].rstrip()
    return flat


def render_changes(changes) -> str:
    """`stop:event_14|reduce_to:event_21:100`, or `none`."""
    rendered = [c.render() for c in (changes or ())]
    return "|".join(rendered) if rendered else "none"


def decision_to_row(decision: Decision, request: Request) -> dict:
    """The eight OUTPUT_COLUMNS as strings, in order."""
    earliest: Optional[object] = decision.earliest_date_for_full_payment
    return {
        "request_id": request.request_id,
        "amount_safe_to_pay": fmt_safe_amount(decision.amount_safe_to_pay),
        "affordability_status": str(decision.affordability_status),
        "recommended_payment_method": str(decision.recommended_payment_method),
        "payment_plan": fmt_plan(list(decision.payment_plan or ())),
        "earliest_date_for_full_payment": fmt_date_iso(earliest) if earliest else "",
        "spending_changes_needed": render_changes(decision.spending_changes_needed),
        "decision_explanation": _clean_explanation(decision.decision_explanation),
    }


def refusal_row(request: Request, reason: str = "") -> dict:
    """The ONLY blanket-refusal row (CONTRACT Section 5 item 9)."""
    explanation = REFUSAL_EXPLANATION
    reason = " ".join(str(reason or "").split())
    if reason:
        explanation = f"{explanation} {reason}"
    return {
        "request_id": request.request_id,
        "amount_safe_to_pay": "0",
        "affordability_status": "not_affordable",
        "recommended_payment_method": "not_recommended",
        "payment_plan": "none",
        "earliest_date_for_full_payment": "",
        "spending_changes_needed": "none",
        "decision_explanation": _clean_explanation(explanation),
    }


def write_output_csv(rows: list[dict], path: str) -> None:
    """Exact column order, QUOTE_MINIMAL, '\\n' line endings."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=list(OUTPUT_COLUMNS),
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({c: str(row.get(c, "")) for c in OUTPUT_COLUMNS})


def read_output_csv(path: str) -> list[dict]:
    """Read back an output.csv as a list of column-ordered dicts."""
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [{c: (row.get(c) or "") for c in OUTPUT_COLUMNS} for row in reader]
