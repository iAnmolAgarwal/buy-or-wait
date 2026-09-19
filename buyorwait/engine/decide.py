"""Turn the forecast into a Decision (docs/CONTRACT.md Section 2, engine/decide.py)."""
from __future__ import annotations

from typing import Optional, Sequence

from buyorwait.types import (
    CashFlow,
    Decision,
    EvidenceSet,
    PaymentOption,
    Profile,
    RecurringStream,
    Request,
)
from buyorwait.engine.explain import explain
from buyorwait.engine.plans import enumerate_plans, rank, rank_key
from buyorwait.engine.policy import Policy
from buyorwait.engine.solver import amount_safe_to_pay, earliest_full_payment_date
from buyorwait.formatting import fmt_amount, fmt_date_iso


def _status_for(plan, earliest=None) -> str:
    if plan is None:
        # D14: PS:122 reserves not_affordable for requests that cannot be completed
        # safely *within the forecast period*. When `earliest` exists the money is
        # there -- only the accepted methods are missing -- so the request is
        # affordable_later with no recommendation.
        return "not_affordable" if earliest is None else "affordable_later"
    if plan.method == "wait":
        return "affordable_later"
    if plan.method == "full_payment" and not plan.changes:
        return "affordable_now"
    return "affordable_with_plan"


def _summary(plan) -> str:
    pays = "|".join(f"{fmt_date_iso(d)}:{fmt_amount(a)}" for d, a in plan.payments) or "none"
    chg = "|".join(c.render() for c in plan.changes) or "none"
    opt = plan.option_id or "-"
    return f"{plan.method}[{opt}] {pays} changes={chg} total={fmt_amount(plan.total_paid)}"


def decide(
    request: Request,
    profile: Profile,
    options: Sequence[PaymentOption],
    streams: Sequence[RecurringStream],
    flows: Sequence[CashFlow],
    evidence: EvidenceSet,
    policy: Policy,
) -> Decision:
    notes: list[str] = []

    safe = amount_safe_to_pay(profile, flows, request, policy)
    earliest = earliest_full_payment_date(profile, flows, request, policy)
    notes.append(
        f"amount_safe_to_pay={fmt_amount(safe)}; "
        f"earliest_date_for_full_payment={fmt_date_iso(earliest) if earliest else '(none in 90 days)'}"
    )

    plans = enumerate_plans(
        profile, request, options, flows, streams, safe, earliest, policy, notes=notes
    )
    best = rank(plans)
    status = _status_for(best, earliest)

    if best is None:
        notes.append(f"no eligible safe plan -> {status} / not_recommended")
        method: str = "not_recommended"
        payments: list = []
        changes: list = []
    else:
        method = best.method
        payments = list(best.payments)
        changes = list(best.changes)
        notes.append(f"chosen: {_summary(best)} (min balance {fmt_amount(best.min_balance_after)})")
        for p in sorted(plans, key=rank_key):
            if p is best:
                continue
            notes.append(f"rejected: {_summary(p)} ranked below the chosen plan")

    if status == "affordable_now":
        # By construction earliest must be request_date here; assert it in the log
        # rather than silently disagreeing with the chosen plan.
        if earliest != request.request_date:
            notes.append(
                "coherence fix: full payment is safe today, so earliest_date_for_full_payment = request_date"
            )
            earliest = request.request_date

    explanation = explain(request, profile, best, status, safe, streams, earliest)

    return Decision(
        request_id=request.request_id,
        amount_safe_to_pay=min(safe, float(request.requested_amount)),
        affordability_status=status,
        recommended_payment_method=method,
        payment_plan=payments,
        earliest_date_for_full_payment=earliest,
        spending_changes_needed=changes,
        decision_explanation=explanation,
        evidence=evidence,
        chosen_plan=best,
        notes=notes,
    )
