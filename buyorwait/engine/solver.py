"""The two preference-independent numbers: how much is safe today, and when the
full amount first becomes safe.

docs/CONTRACT.md Section 2, engine/solver.py. Neither function looks at
`profile.methods` or at spending changes (PS:163, PS:183).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

from buyorwait.types import CashFlow, Profile, Request
from buyorwait.engine.forecast import balance_path
from buyorwait.engine.policy import Policy


def floor2(x: float) -> float:
    """Round *down* to 2 decimals. amount_safe_to_pay must never overshoot, so
    a half-up round is not safe here."""
    return math.floor(round(float(x) * 100.0, 6)) / 100.0


def min_balance(
    profile: Profile,
    flows: Iterable[CashFlow],
    start: date,
    horizon_days: int,
    extra_payments: Sequence[tuple[date, float]] = (),
) -> float:
    return min(b for _, b in balance_path(profile, flows, start, horizon_days, extra_payments))


def path_is_safe(
    profile: Profile,
    flows: Iterable[CashFlow],
    start: date,
    horizon_days: int,
    extra_payments: Sequence[tuple[date, float]],
    policy: Policy,
) -> bool:
    return policy.meets_minimum(
        min_balance(profile, flows, start, horizon_days, extra_payments),
        float(profile.minimum_balance_to_keep),
    )


def amount_safe_to_pay(
    profile: Profile,
    flows: Iterable[CashFlow],
    request: Request,
    policy: Policy,
) -> float:
    """Largest x in [0, requested_amount] such that paying x on request_date keeps
    every day of the 90-day path at or above minimum_balance_to_keep.

    A payment on day 0 lowers every later day equally, so the answer is a closed
    form: clamp(min(path) - minimum, 0, requested), floored to 2 dp."""
    flows = list(flows or ())
    headroom = min_balance(profile, flows, request.request_date, policy.horizon_days) - float(
        profile.minimum_balance_to_keep
    )
    if policy.strict_minimum:
        headroom -= 0.01
    x = max(0.0, min(headroom, float(request.requested_amount)))
    return floor2(x)


def check_horizon_for(request: Request, d: date, policy: Policy) -> int:
    """How many days of the path a single full payment on `d` must keep safe.

    "tail" and "full" both cover the whole horizon -- under "tail" it is the flow
    *list* that is trimmed, not the window (see `tail_trim_from`). "deadline" is
    the superseded D25 rule: request_date .. max(d, desired_completion_date).

    `earliest` and the `wait` plan must agree, so both call this."""
    i = (d - request.request_date).days
    if i <= 0:
        # D27: paying today is exactly what amount_safe_to_pay measures, so it is
        # held to the same full-horizon check. That keeps the two columns in step
        # -- earliest == request_date iff the full amount is safe today -- and
        # stops the row failing the coherence gate.
        return policy.horizon_days
    if policy.earliest_window == "deadline":
        to_deadline = (request.desired_completion_date - request.request_date).days
        return min(policy.horizon_days, max(i, to_deadline))
    return policy.horizon_days


def tail_trim_from(request: Request, d: date, policy: Policy) -> Optional[date]:
    """D35: the date from which *projected* occurrences are ignored for a payment
    on `d`, or None when nothing is trimmed.

    Only under earliest_window="tail", and never for the request-date candidate
    (D27 keeps that one on the untrimmed forecast)."""
    if policy.earliest_window != "tail":
        return None
    if (d - request.request_date).days <= 0:
        return None
    return request.request_date + timedelta(days=policy.horizon_days - policy.tail_days)


def trim_tail_recurring(flows: Iterable[CashFlow], tail_from: Optional[date]) -> list[CashFlow]:
    """Drop projected stream occurrences on/after `tail_from`. Confirmed flows --
    scheduled events and anything derived from a message or an image -- always
    stay: a known debit is a fact on day 88 just as much as on day 8."""
    flows = list(flows or ())
    if tail_from is None:
        return flows
    return [f for f in flows if not (f.kind == "recurring" and f.flow_date >= tail_from)]


def flows_for_candidate(
    flows: Iterable[CashFlow], request: Request, d: date, policy: Policy
) -> list[CashFlow]:
    """The flow list the safety check for a full payment on `d` runs against."""
    return trim_tail_recurring(flows, tail_trim_from(request, d, policy))


def earliest_full_payment_date(
    profile: Profile,
    flows: Iterable[CashFlow],
    request: Request,
    policy: Policy,
) -> Optional[date]:
    """First date d in the horizon on which paying the whole requested_amount keeps
    the entire path safe. Preference-independent; no spending changes."""
    flows = list(flows or ())
    start = request.request_date
    requested = float(request.requested_amount)
    for i in range(policy.horizon_days + 1):
        d = start + timedelta(days=i)
        window = check_horizon_for(request, d, policy)
        check_flows = flows_for_candidate(flows, request, d, policy)
        if path_is_safe(profile, check_flows, start, window, [(d, requested)], policy):
            return d
    return None
