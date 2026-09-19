"""validate/schema.py — surface validation of one output.csv row.

Checks column set/order, enums, number formats and the payment_plan /
spending_changes_needed mini-grammars from PS:84-163 and CONTRACT Section 3.
Returns a list of human-readable problems; [] means the row is well formed.

Schema validation knows nothing about the forecast: it only needs the row and
the Request it is supposed to answer.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from buyorwait.types import METHODS, OUTPUT_COLUMNS, STATUSES, Request

# Amount as written anywhere in the output columns: integer or 1-2 decimals.
AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")

MAX_EXPLANATION = 400
MAX_CHANGES = 3
AMOUNT_TOLERANCE = 0.005


def parse_iso_date(text: str) -> Optional[date]:
    if not DATE_RE.match(text or ""):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_plan(text: str) -> tuple[list[tuple[date, float]], list[str]]:
    """Parse a payment_plan cell. Returns (payments, problems).

    `none` parses to an empty list with no problems.
    """
    problems: list[str] = []
    if text == "none":
        return [], problems
    if text == "":
        return [], ["payment_plan: empty (use 'none' when no payment is recommended)"]

    payments: list[tuple[date, float]] = []
    for i, segment in enumerate(text.split("|")):
        parts = segment.split(":")
        if len(parts) != 2:
            problems.append(
                f"payment_plan: segment {i + 1} {segment!r} is not '<YYYY-MM-DD>:<amount>'"
            )
            continue
        raw_date, raw_amount = parts
        d = parse_iso_date(raw_date)
        if d is None:
            problems.append(f"payment_plan: segment {i + 1} has a bad date {raw_date!r}")
        if not AMOUNT_RE.match(raw_amount):
            problems.append(
                f"payment_plan: segment {i + 1} has a bad amount {raw_amount!r}"
            )
            continue
        amount = float(raw_amount)
        if amount <= 0:
            problems.append(f"payment_plan: segment {i + 1} amount must be > 0")
        if d is not None:
            payments.append((d, amount))

    dates = [d for d, _ in payments]
    if any(b < a for a, b in zip(dates, dates[1:])):
        problems.append("payment_plan: payments are not in chronological order")
    return payments, problems


def parse_changes(text: str) -> tuple[list[tuple[str, str, Optional[float]]], list[str]]:
    """Parse spending_changes_needed. Returns ([(kind, event_id, amount)], problems)."""
    problems: list[str] = []
    if text == "none":
        return [], problems
    if text == "":
        return [], ["spending_changes_needed: empty (use 'none' when no change is needed)"]

    changes: list[tuple[str, str, Optional[float]]] = []
    segments = text.split("|")
    if len(segments) > MAX_CHANGES:
        problems.append(
            f"spending_changes_needed: {len(segments)} changes, at most {MAX_CHANGES} allowed"
        )
    for i, segment in enumerate(segments):
        parts = segment.split(":")
        if parts[0] == "stop":
            if len(parts) != 2 or not EVENT_ID_RE.match(parts[1]):
                problems.append(
                    f"spending_changes_needed: segment {i + 1} {segment!r} is not 'stop:<event_id>'"
                )
                continue
            changes.append(("stop", parts[1], None))
        elif parts[0] == "reduce_to":
            if len(parts) != 3 or not EVENT_ID_RE.match(parts[1]):
                problems.append(
                    f"spending_changes_needed: segment {i + 1} {segment!r} is not "
                    "'reduce_to:<event_id>:<amount>'"
                )
                continue
            if not AMOUNT_RE.match(parts[2]):
                problems.append(
                    f"spending_changes_needed: segment {i + 1} has a bad amount {parts[2]!r}"
                )
                continue
            changes.append(("reduce_to", parts[1], float(parts[2])))
        else:
            problems.append(
                f"spending_changes_needed: segment {i + 1} {segment!r} has an unknown kind "
                f"{parts[0]!r} (expected 'stop' or 'reduce_to')"
            )

    event_ids = [eid for _, eid, _ in changes]
    if len(set(event_ids)) != len(event_ids):
        problems.append(
            "spending_changes_needed: the same event_id is referenced by more than one change"
        )
    return changes, problems


def validate_row(row: dict, request: Request) -> list[str]:
    """Validate one output row against the schema. [] when valid."""
    problems: list[str] = []

    keys = list(row.keys())
    if keys != list(OUTPUT_COLUMNS):
        missing = [c for c in OUTPUT_COLUMNS if c not in keys]
        extra = [c for c in keys if c not in OUTPUT_COLUMNS]
        if missing:
            problems.append(f"columns: missing {missing}")
        if extra:
            problems.append(f"columns: unexpected {extra}")
        if not missing and not extra:
            problems.append(f"columns: wrong order {keys} != {list(OUTPUT_COLUMNS)}")
        if missing:
            # Without the columns there is nothing else worth checking.
            return problems

    type_problems = [
        f"{c}: value must be a string, got {type(row.get(c)).__name__}"
        for c in OUTPUT_COLUMNS
        if not isinstance(row.get(c), str)
    ]
    if type_problems:
        return problems + type_problems

    # --- request_id ---------------------------------------------------------
    if row["request_id"] != request.request_id:
        problems.append(
            f"request_id: {row['request_id']!r} does not echo {request.request_id!r}"
        )

    # --- amount_safe_to_pay -------------------------------------------------
    raw_safe = row["amount_safe_to_pay"]
    safe: Optional[float] = None
    if not AMOUNT_RE.match(raw_safe):
        problems.append(f"amount_safe_to_pay: {raw_safe!r} is not a plain number")
    else:
        safe = float(raw_safe)
        if safe < -AMOUNT_TOLERANCE:
            problems.append("amount_safe_to_pay: must be >= 0")
        if safe > request.requested_amount + AMOUNT_TOLERANCE:
            problems.append(
                f"amount_safe_to_pay: {safe} exceeds requested_amount "
                f"{request.requested_amount}"
            )

    # --- enums --------------------------------------------------------------
    status = row["affordability_status"]
    method = row["recommended_payment_method"]
    if status not in STATUSES:
        problems.append(f"affordability_status: {status!r} not in {sorted(STATUSES)}")
    if method not in METHODS:
        problems.append(f"recommended_payment_method: {method!r} not in {sorted(METHODS)}")

    # --- payment_plan -------------------------------------------------------
    _payments, plan_problems = parse_plan(row["payment_plan"])
    problems.extend(plan_problems)

    # --- earliest_date_for_full_payment -------------------------------------
    raw_earliest = row["earliest_date_for_full_payment"]
    if raw_earliest != "" and parse_iso_date(raw_earliest) is None:
        problems.append(
            f"earliest_date_for_full_payment: {raw_earliest!r} is not YYYY-MM-DD or empty"
        )

    # --- spending_changes_needed -------------------------------------------
    _changes, change_problems = parse_changes(row["spending_changes_needed"])
    problems.extend(change_problems)

    # --- decision_explanation ----------------------------------------------
    explanation = row["decision_explanation"]
    if not explanation.strip():
        problems.append("decision_explanation: must not be empty")
    if "\n" in explanation or "\r" in explanation:
        problems.append("decision_explanation: must not contain a newline")
    if len(explanation) > MAX_EXPLANATION:
        problems.append(
            f"decision_explanation: {len(explanation)} chars, at most {MAX_EXPLANATION} allowed"
        )

    return problems
