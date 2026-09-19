"""Lifecycle resolution: which events may enter the 90-day forecast.

Contract Section 2 (`resolve_lifecycles`) and Section 5 (edge-case policy);
problem_statement.md lines 36, 178, 200-205.
"""
from __future__ import annotations

from typing import Optional

from buyorwait.types import Event

EXCLUDED_STATUSES = {"cancelled", "failed"}


def _numeric_id(s: str) -> tuple[int, str]:
    import re

    m = re.findall(r"(\d+)", s or "")
    return (int(m[-1]) if m else -1, s or "")


def exclusion_reason(event: Event) -> Optional[str]:
    """Context-free exclusion test for a single event.

    Returns a short machine-readable reason, or None when the event is eligible.
    Rules that need a second event (duplicates, linked lifecycles) live in
    `resolve_lifecycles`.
    """
    if event.status in EXCLUDED_STATUSES:
        return event.status                      # "cancelled" / "failed"
    if event.status == "unrealized":
        return "unrealized"
    if event.direction == "non_cash":
        return "non_cash"
    if event.event_type == "investment_valuation":
        return "investment_valuation"
    if event.status == "pending" and event.direction == "credit":
        return "pending_credit"
    return None


def _is_duplicate_of(event: Event, target: Event) -> bool:
    """A later pending debit that repeats an earlier debit of the same amount and
    category is a double-charge record, not a second real payment (PS:178)."""
    if event.status != "pending" or event.direction != "debit":
        return False
    if target.direction != "debit":
        return False
    if event.category != target.category:
        return False
    if event.amount is None or target.amount is None:
        return False
    if abs(event.amount - target.amount) > 0.005:
        return False
    return event.event_date >= target.event_date


def resolve_lifecycles(events: list[Event]) -> tuple[list[Event], list[str]]:
    """Return (forecast-eligible events, notes).

    Never raises. Order of the input list is preserved in the output.
    """
    notes: list[str] = []
    by_id = {e.event_id: e for e in events}
    dropped: dict[str, str] = {}

    # 1. Context-free exclusions.
    for e in events:
        reason = exclusion_reason(e)
        if reason:
            dropped[e.event_id] = reason
            notes.append(f"{e.event_id}: excluded ({reason})")

    # 2. linked_event_id lifecycles. The linking event is the newer record.
    for e in events:
        link = e.linked_event_id
        if not link:
            continue
        target = by_id.get(link)
        if target is None:
            notes.append(f"{e.event_id}: linked_event_id {link} not found; treated standalone")
            continue
        if e.status == "cancelled":
            # An explicit cancellation record cancels its target (PS:200-205).
            if target.event_id not in dropped:
                dropped[target.event_id] = "cancelled_by_link"
                notes.append(f"{target.event_id}: excluded (cancelled by {e.event_id})")
            continue
        if e.event_id in dropped:
            continue
        if _is_duplicate_of(e, target):
            dropped[e.event_id] = "duplicate_of_" + target.event_id
            notes.append(f"{e.event_id}: excluded (duplicate of {target.event_id})")
            continue
        if target.status in EXCLUDED_STATUSES:
            # retry / re-issue: the newer record replaces the failed or cancelled one
            notes.append(
                f"{e.event_id}: replaces {target.event_id} ({target.status}); newer record kept"
            )
        else:
            notes.append(f"{e.event_id}: linked to {target.event_id}; both real, both kept")

    # 3. Exact duplicate rows: keep the first event_id only.
    seen: dict[tuple, str] = {}
    for e in sorted(events, key=lambda x: _numeric_id(x.event_id)):
        if e.event_id in dropped:
            continue
        key = (e.user_id, e.amount, e.event_date, e.category, e.direction)
        first = seen.get(key)
        if first is None:
            seen[key] = e.event_id
        else:
            dropped[e.event_id] = "duplicate_of_" + first
            notes.append(f"{e.event_id}: excluded (duplicate row of {first})")

    eligible = [e for e in events if e.event_id not in dropped]
    return eligible, notes
