"""decision_explanation templates (docs/CONTRACT.md Section 4).

Facts only: every number here comes from the Decision (plan payments, the safe
amount, the requested amount) or from profile.minimum_balance_to_keep, and every
name comes from a stream that the plan actually changes.
"""
from __future__ import annotations

from typing import Optional, Sequence

from buyorwait.types import Plan, Profile, RecurringStream, Request, SpendingChange
from buyorwait.formatting import fmt_date_long, fmt_money


def _desc(stream_id: str, streams: Sequence[RecurringStream]) -> str:
    for s in streams or ():
        if s.stream_id == stream_id:
            return (s.description or "expense").strip().lower()
    return "expense"


def render_changes(changes: Sequence[SpendingChange], streams: Sequence[RecurringStream], ccy: str) -> str:
    """'Stop the online backup subscription and reduce the streaming subscription
    to USD 23.50' -- first clause capitalised, the rest lower-case."""
    parts: list[str] = []
    for c in changes:
        d = _desc(c.stream_id, streams)
        if c.kind == "stop":
            parts.append(f"stop the {d}")
        else:
            parts.append(f"reduce the {d} to {fmt_money(ccy, c.new_amount or 0.0)}")
    if not parts:
        return ""
    joined = " and ".join(parts)
    return joined[0].upper() + joined[1:]


def _action(plan: Plan, ccy: str) -> str:
    if plan.method == "installments":
        n = len(plan.payments)
        amt = plan.payments[0][1]
        return f"use {n} installments of {fmt_money(ccy, amt)}, starting {fmt_date_long(plan.payments[0][0])}"
    return f"pay {fmt_money(ccy, plan.payments[0][1])} today"


def explain(
    request: Request,
    profile: Profile,
    plan: Optional[Plan],
    status: str,
    safe: float,
    streams: Sequence[RecurringStream] = (),
    earliest=None,
) -> str:
    ccy = profile.home_currency
    minimum = float(profile.minimum_balance_to_keep)

    # D14: no eligible plan, but the full amount does become safe inside the
    # horizon -- the user simply does not accept the method that would use it.
    if plan is None and status == "affordable_later" and earliest is not None:
        return (
            f"Do not proceed with the {fmt_money(ccy, request.requested_amount)} request using the "
            f"accepted methods. The full amount is forecast to be safe as a single payment from "
            f"{fmt_date_long(earliest)}."
        )

    if plan is None or status == "not_affordable":
        if safe > 0:
            return (
                f"Do not proceed with the {fmt_money(ccy, request.requested_amount)} request. "
                f"Although {fmt_money(ccy, safe)} is available today, the full amount cannot be "
                f"completed safely within 90 days."
            )
        return (
            f"Do not make this payment by {fmt_date_long(request.desired_completion_date)}. "
            f"None of the available options keeps the {fmt_money(ccy, minimum)} minimum protected."
        )

    if plan.changes:
        prefix = render_changes(plan.changes, streams, ccy)
        return f"{prefix}, then {_action(plan, ccy)}. This leaves at least {fmt_money(ccy, minimum)} available."

    if plan.method == "full_payment":
        return (
            f"Pay {fmt_money(ccy, plan.payments[0][1])} today. "
            f"This leaves at least {fmt_money(ccy, minimum)} available over the next 90 days."
        )

    if plan.method == "installments":
        n = len(plan.payments)
        return (
            f"Use {n} installments of {fmt_money(ccy, plan.payments[0][1])}, "
            f"starting {fmt_date_long(plan.payments[0][0])}. "
            f"This leaves at least {fmt_money(ccy, minimum)} available."
        )

    if plan.method == "partial_payment":
        (d0, a0), (d1, a1) = plan.payments[0], plan.payments[1]
        return (
            f"Pay {fmt_money(ccy, a0)} today and the remaining {fmt_money(ccy, a1)} on {fmt_date_long(d1)}. "
            f"This completes the full request and keeps the {fmt_money(ccy, minimum)} minimum protected."
        )

    if plan.method == "wait":
        d, a = plan.payments[0]
        return (
            f"Pay {fmt_money(ccy, a)} in full on {fmt_date_long(d)}. "
            f"Paying earlier would take the balance below the {fmt_money(ccy, minimum)} minimum."
        )

    return (
        f"Do not make this payment by {fmt_date_long(request.desired_completion_date)}. "
        f"None of the available options keeps the {fmt_money(ccy, minimum)} minimum protected."
    )
