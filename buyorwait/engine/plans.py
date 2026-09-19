"""Candidate plan enumeration and ranking.

docs/CONTRACT.md Section 2, engine/plans.py; PS:146 (partial), PS:183 (no
changes for wait/partial), PS:189 (wait), PS:191-196 (ranking), PS:198 (never
stop+reduce the same event).
"""
from __future__ import annotations

import dataclasses
import itertools
from datetime import date, timedelta
from typing import Optional, Sequence

from buyorwait.types import CashFlow, PaymentOption, Plan, Profile, RecurringStream, Request, SpendingChange
from buyorwait.engine.policy import Policy
from buyorwait.engine.solver import check_horizon_for, flows_for_candidate, min_balance

MAX_CHANGES = 3
# Only the most valuable candidate changes are searched, so the subset scan stays
# bounded (C(10,3) = 120 simulations worst case) while remaining deterministic.
_MAX_CANDIDATES = 10


def _evnum(s: Optional[str]) -> int:
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    return int(digits) if digits else 0


# ---------------------------------------------------------------------------
# Spending changes
# ---------------------------------------------------------------------------


def apply_changes(flows: Sequence[CashFlow], changes: Sequence[SpendingChange]) -> list[CashFlow]:
    """A change applies to *every* future occurrence of its stream in the horizon."""
    if not changes:
        return list(flows)
    by_stream = {c.stream_id: c for c in changes}
    out: list[CashFlow] = []
    for f in flows:
        c = by_stream.get(f.stream_id) if f.stream_id else None
        if c is None:
            out.append(f)
            continue
        if c.kind == "stop":
            continue
        new_mag = abs(float(c.new_amount or 0.0))
        out.append(dataclasses.replace(f, amount=new_mag if f.amount > 0 else -new_mag))
    return out


def candidate_changes(
    profile: Profile,
    streams: Sequence[RecurringStream],
    flows: Sequence[CashFlow],
) -> list[tuple[SpendingChange, float]]:
    """(change, horizon saving) for every change the profile *and* the stream's
    flexibility both permit. Sorted by saving desc, then stream id for stability."""
    by_stream: dict[str, list[CashFlow]] = {}
    for f in flows:
        if f.stream_id and f.amount < 0:
            by_stream.setdefault(f.stream_id, []).append(f)

    reduce_cats = set(profile.reduce_categories or ())
    stop_cats = set(profile.stop_categories or ())
    out: list[tuple[SpendingChange, float]] = []
    for s in streams or ():
        if s.direction != "debit":
            continue
        occ = by_stream.get(s.stream_id)
        if not occ:
            continue
        can_stop = s.flexibility in ("stoppable", "reducible_or_stoppable") and s.category in stop_cats
        can_reduce = (
            s.flexibility in ("reducible", "reducible_or_stoppable")
            and s.category in reduce_cats
            and s.minimum_allowed_amount is not None
        )
        if can_stop:
            saving = sum(abs(f.amount) for f in occ)
            if saving > 0:
                out.append((SpendingChange("stop", s.anchor_event_id, None, s.stream_id), saving))
        if can_reduce:
            floor = float(s.minimum_allowed_amount)
            saving = sum(max(0.0, abs(f.amount) - floor) for f in occ)
            if saving > 0:
                out.append((SpendingChange("reduce_to", s.anchor_event_id, floor, s.stream_id), saving))
    out.sort(key=lambda t: (-t[1], t[0].stream_id, t[0].kind))
    return out[:_MAX_CANDIDATES]


def _smallest_safe_change_set(
    profile: Profile,
    flows: Sequence[CashFlow],
    request: Request,
    policy: Policy,
    payments: Sequence[tuple[date, float]],
    candidates: Sequence[tuple[SpendingChange, float]],
) -> Optional[tuple[list[SpendingChange], float]]:
    """Fewest changes first, then the largest total saving. Never two changes on
    the same stream (so never stop+reduce the same event, PS:198)."""
    minimum = float(profile.minimum_balance_to_keep)
    for k in range(1, MAX_CHANGES + 1):
        if k > len(candidates):
            break
        best = None
        for combo in itertools.combinations(candidates, k):
            sids = {c.stream_id for c, _ in combo}
            if len(sids) != k:
                continue
            chs = [c for c, _ in combo]
            mb = min_balance(
                profile, apply_changes(flows, chs), request.request_date, policy.horizon_days, payments
            )
            if not policy.meets_minimum(mb, minimum):
                continue
            saving = sum(sv for _, sv in combo)
            key = (-saving, tuple(sorted(_evnum(c.event_id) for c in chs)))
            if best is None or key < best[0]:
                best = (key, chs, mb)
        if best is not None:
            chs = sorted(best[1], key=lambda c: (_evnum(c.event_id), c.event_id))
            return chs, best[2]
    return None


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _make_plan(
    profile: Profile,
    request: Request,
    policy: Policy,
    flows: Sequence[CashFlow],
    method: str,
    payments: list[tuple[date, float]],
    changes: list[SpendingChange],
    option_id: Optional[str],
    total_paid: float,
    horizon: Optional[int] = None,
    check_flows: Optional[Sequence[CashFlow]] = None,
) -> Plan:
    # Payments beyond the horizon are not simulated (they are outside the forecast).
    horizon = policy.horizon_days if horizon is None else horizon
    base = flows if check_flows is None else check_flows
    end = request.request_date + timedelta(days=horizon)
    sim = [(d, a) for d, a in payments if d <= end]
    mb = min_balance(profile, apply_changes(base, changes), request.request_date, horizon, sim)
    last = max(d for d, _ in payments) if payments else request.request_date
    return Plan(
        method=method,
        payments=list(payments),
        changes=list(changes),
        option_id=option_id,
        total_paid=round(float(total_paid), 2),
        completes_by_deadline=last <= request.desired_completion_date,
        min_balance_after=mb,
    )


def enumerate_plans(
    profile: Profile,
    request: Request,
    options: Sequence[PaymentOption],
    flows: Sequence[CashFlow],
    streams: Sequence[RecurringStream],
    safe_today: float,
    earliest: Optional[date],
    policy: Policy,
    *,
    notes: Optional[list[str]] = None,
) -> list[Plan]:
    """Every eligible plan that passes the 90-day safety check.

    `notes` (optional) collects one line per rejected candidate."""

    def reject(msg: str) -> None:
        if notes is not None:
            notes.append(msg)

    flows = list(flows or ())
    methods = set(profile.methods or ())
    minimum = float(profile.minimum_balance_to_keep)
    requested = float(request.requested_amount)
    req_date = request.request_date
    deadline = request.desired_completion_date
    cands = candidate_changes(profile, streams or (), flows)
    plans: list[Plan] = []

    def safe(payments, changes=(), horizon: Optional[int] = None, check_flows=None) -> bool:
        horizon = policy.horizon_days if horizon is None else horizon
        base = flows if check_flows is None else check_flows
        end = req_date + timedelta(days=horizon)
        sim = [(d, a) for d, a in payments if d <= end]
        return policy.meets_minimum(
            min_balance(profile, apply_changes(base, list(changes)), req_date, horizon, sim),
            minimum,
        )

    # --- full_payment ------------------------------------------------------
    if "full_payment" in methods:
        pay = [(req_date, requested)]
        if policy.deadline_is_hard and req_date > deadline:
            reject("full_payment rejected: request_date is after desired_completion_date")
        elif safe(pay):
            plans.append(_make_plan(profile, request, policy, flows, "full_payment", pay, [], None, requested))
        else:
            found = _smallest_safe_change_set(profile, flows, request, policy, pay, cands)
            if found:
                plans.append(
                    _make_plan(profile, request, policy, flows, "full_payment", pay, found[0], None, requested)
                )
            else:
                reject("full_payment rejected: breaches the minimum balance, no permitted spending changes fix it")
    else:
        reject("full_payment rejected: not in payment_methods_user_will_consider")

    # --- partial_payment (PS:146) -----------------------------------------
    if "partial_payment" not in methods:
        reject("partial_payment rejected: not in payment_methods_user_will_consider")
    elif not request.allows_partial_payment:
        reject("partial_payment rejected: request does not allow partial payment")
    elif not (0 < safe_today < requested):
        reject("partial_payment rejected: amount_safe_to_pay is not strictly between 0 and the requested amount")
    elif earliest is None:
        reject("partial_payment rejected: the full amount never becomes safe in the horizon")
    elif earliest > deadline:
        reject("partial_payment rejected: earliest_date_for_full_payment is after desired_completion_date")
    else:
        pay = [(req_date, floor_amt(safe_today)), (earliest, round(requested - floor_amt(safe_today), 2))]
        if safe(pay):
            plans.append(_make_plan(profile, request, policy, flows, "partial_payment", pay, [], None, requested))
        else:
            reject("partial_payment rejected: the two-payment schedule still breaches the minimum balance")

    # --- installments ------------------------------------------------------
    inst_options = [o for o in (options or ()) if o.payment_method == "installments"]
    if "installments" not in methods:
        if inst_options:
            reject("installments rejected: not in payment_methods_user_will_consider")
    else:
        for opt in sorted(inst_options, key=lambda o: _evnum(o.payment_option_id)):
            sched = opt.schedule()
            if not sched:
                reject(f"{opt.payment_option_id} rejected: empty schedule")
                continue
            if (
                policy.installments_cap_by_months
                and profile.max_installment_months is not None
                and opt.number_of_payments > int(profile.max_installment_months)
            ):
                reject(
                    f"{opt.payment_option_id} rejected: {opt.number_of_payments} payments exceeds "
                    f"max_installment_months={profile.max_installment_months}"
                )
                continue
            last = max(d for d, _ in sched)
            if policy.deadline_is_hard and last > deadline:
                reject(f"{opt.payment_option_id} rejected: last payment {last} is after the desired completion date")
                continue
            total = float(opt.total_payable_amount or sum(a for _, a in sched))
            if safe(sched):
                plans.append(
                    _make_plan(
                        profile, request, policy, flows, "installments", sched, [], opt.payment_option_id, total
                    )
                )
                continue
            found = _smallest_safe_change_set(profile, flows, request, policy, sched, cands)
            if found:
                plans.append(
                    _make_plan(
                        profile, request, policy, flows, "installments", sched, found[0], opt.payment_option_id, total
                    )
                )
            else:
                reject(f"{opt.payment_option_id} rejected: breaches the minimum balance even with spending changes")

    # --- wait (PS:189) -----------------------------------------------------
    if earliest is None:
        reject("wait rejected: the full amount never becomes safe within 90 days")
    elif earliest <= req_date:
        reject("wait rejected: the full amount is already safe today")
    elif "full_payment" not in methods:
        reject("wait rejected: the user does not accept full_payment")
    else:
        pay = [(earliest, requested)]
        # D25/D35: the wait plan is exactly "pay in full on `earliest`", so it is
        # judged over the same window AND the same trimmed flow list that
        # produced `earliest` -- otherwise the two columns could disagree.
        window = check_horizon_for(request, earliest, policy)
        wait_flows = flows_for_candidate(flows, request, earliest, policy)
        if safe(pay, horizon=window, check_flows=wait_flows):
            plans.append(
                _make_plan(
                    profile, request, policy, flows, "wait", pay, [], None, requested,
                    window, wait_flows,
                )
            )
        else:  # unreachable by construction of `earliest`, kept as a guard
            reject("wait rejected: paying on the earliest safe date still breaches the minimum balance")

    return plans


def floor_amt(x: float) -> float:
    return round(float(x), 2)


# ---------------------------------------------------------------------------
# Ranking (PS:191-196)
# ---------------------------------------------------------------------------


def rank_key(p: Plan):
    return (
        0 if p.completes_by_deadline else 1,          # 1. complete by desired_completion_date
        1 if p.changes else 0,                        # 2. require no spending changes
        round(float(p.total_paid), 2),                # 3. minimise total paid
        p.start if p.start is not None else date.max,  # 4. start earlier
        len(p.payments),                              # 5. fewer payments
        _evnum(p.option_id) if p.option_id else -1,   # 6. lowest payment_option_id
    )


def rank(plans: Sequence[Plan]) -> Optional[Plan]:
    """Best plan by the six PS criteria, or None when nothing is eligible."""
    if not plans:
        return None
    return sorted(plans, key=rank_key)[0]
