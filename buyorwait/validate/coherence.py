"""validate/coherence.py — internal consistency of a Decision.

Every rule here comes from CONTRACT Section 2 (`validate/coherence`) and
PS:84-163 / PS:187-198. Each rule emits a problem string prefixed with a stable
tag (`status/method`, `partial`, `installments`, ...) so callers and tests can
match on it.

Depends only on the Decision, its Request and the user's Profile.
"""
from __future__ import annotations

from typing import Optional

from buyorwait.formatting import fmt_amount, fmt_money
from buyorwait.types import METHODS, STATUSES, Decision, Profile, Request

TOL = 0.005
MAX_CHANGES = 3
MAX_EXPLANATION = 400

IMMEDIATE_METHODS = {"full_payment", "partial_payment", "installments"}
CHANGE_METHODS = {"full_payment", "installments"}

# Orchestrator decision D14 (docs/DECISIONS.md): full payment becomes safe inside the
# horizon, but `wait` is ineligible because the user does not accept full_payment
# (PS:189) and no partial/installment plan is safe. The capacity date is still
# reported, the recommendation is still `not_recommended`, and the plan stays empty.
LATER_METHODS = {"wait", "not_recommended"}
NOT_RECOMMENDED_STATUSES = {"not_affordable", "affordable_later"}


def _eq(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= TOL


def _amount_strings(value: float) -> set[str]:
    """Accept either the grouped form (`25,256`) or the plain form (`25256`)."""
    grouped = fmt_money("", value).strip()
    return {grouped, fmt_amount(value)}


def check_coherence(
    decision: Decision, request: Request, profile: Optional[Profile]
) -> list[str]:
    """Return a list of coherence problems; [] when the Decision hangs together."""
    problems: list[str] = []

    safe = float(decision.amount_safe_to_pay)
    requested = float(request.requested_amount)
    status = decision.affordability_status
    method = decision.recommended_payment_method
    plan: list[tuple] = [tuple(p) for p in (decision.payment_plan or ())]
    changes = list(decision.spending_changes_needed or ())
    earliest = decision.earliest_date_for_full_payment
    deadline = request.desired_completion_date
    methods = set(getattr(profile, "methods", None) or ()) if profile else set()

    # --- amount_safe_to_pay -------------------------------------------------
    if safe < -TOL:
        problems.append(f"amount: amount_safe_to_pay {safe} must be >= 0")
    if safe > requested + TOL:
        problems.append(
            f"amount: amount_safe_to_pay {safe} exceeds requested_amount {requested}"
        )

    # --- enums --------------------------------------------------------------
    if status not in STATUSES:
        problems.append(f"enum: affordability_status {status!r} is not allowed")
    if method not in METHODS:
        problems.append(f"enum: recommended_payment_method {method!r} is not allowed")

    # --- plan shape ---------------------------------------------------------
    dates = [d for d, _ in plan]
    if any(b < a for a, b in zip(dates, dates[1:])):
        problems.append("plan: payments are not in chronological order")
    if any(float(a) <= 0 for _, a in plan):
        problems.append("plan: every payment amount must be > 0")

    # --- status <-> method --------------------------------------------------
    if status == "affordable_now" and method != "full_payment":
        problems.append(
            f"status/method: affordable_now requires full_payment, got {method!r}"
        )
    if status == "affordable_later" and method not in LATER_METHODS:
        problems.append(
            f"status/method: affordable_later requires one of {sorted(LATER_METHODS)}, "
            f"got {method!r}"
        )
    if status == "not_affordable" and method != "not_recommended":
        problems.append(
            f"status/method: not_affordable requires not_recommended, got {method!r}"
        )
    if status == "affordable_with_plan" and method not in IMMEDIATE_METHODS:
        problems.append(
            f"status/method: affordable_with_plan requires one of {sorted(IMMEDIATE_METHODS)}, "
            f"got {method!r}"
        )
    if method == "not_recommended" and status not in NOT_RECOMMENDED_STATUSES:
        problems.append(
            f"status/method: not_recommended requires one of "
            f"{sorted(NOT_RECOMMENDED_STATUSES)}, got {status!r}"
        )
    if method == "wait" and status != "affordable_later":
        problems.append(f"status/method: wait requires affordable_later, got {status!r}")

    # --- eligibility against the user's accepted methods --------------------
    if profile is not None:
        # `not_recommended` is deliberately exempt: it is the fallback when nothing is
        # eligible and it never appears in payment_methods_user_will_consider.
        if method in IMMEDIATE_METHODS and method not in methods:
            problems.append(
                f"eligibility: {method!r} is not in the user's accepted payment methods"
            )
        if method == "wait" and "full_payment" not in methods:
            problems.append(
                "eligibility: wait requires the user to accept full_payment (PS:189)"
            )

    # --- affordable_now -----------------------------------------------------
    if status == "affordable_now":
        if earliest != request.request_date:
            problems.append(
                "affordable_now: earliest_date_for_full_payment must equal request_date "
                f"({earliest!r} != {request.request_date!r})"
            )
        if changes:
            problems.append("affordable_now: must not require spending changes")
        if len(plan) != 1 or not plan or plan[0][0] != request.request_date or not _eq(
            plan[0][1] if plan else 0.0, requested
        ):
            problems.append(
                "affordable_now: payment_plan must be exactly the full amount on request_date"
            )

    # --- full_payment -------------------------------------------------------
    if method == "full_payment":
        if len(plan) != 1 or not plan or plan[0][0] != request.request_date or not _eq(
            plan[0][1] if plan else 0.0, requested
        ):
            problems.append(
                "full_payment: payment_plan must be exactly the full amount on request_date"
            )
        expected_status = "affordable_with_plan" if changes else "affordable_now"
        if status != expected_status:
            problems.append(
                f"full_payment: with {len(changes)} spending change(s) the status must be "
                f"{expected_status!r}, got {status!r}"
            )

    # --- wait ---------------------------------------------------------------
    if method == "wait":
        if earliest is None:
            problems.append("wait: earliest_date_for_full_payment must not be empty")
        elif earliest <= request.request_date:
            problems.append(
                "wait: earliest_date_for_full_payment must be after request_date"
            )
        if len(plan) != 1 or not plan or (earliest is not None and plan[0][0] != earliest) \
                or not _eq(plan[0][1] if plan else 0.0, requested):
            problems.append(
                "wait: payment_plan must be exactly the full amount on "
                "earliest_date_for_full_payment"
            )
        if changes:
            problems.append("wait: must not require spending changes (PS:183)")

    # --- partial_payment ----------------------------------------------------
    if method == "partial_payment":
        if status != "affordable_with_plan":
            problems.append(
                f"partial: affordability_status must be affordable_with_plan, got {status!r}"
            )
        if not request.allows_partial_payment:
            problems.append("partial: the request does not allow partial payment")
        if not (0 < safe < requested - TOL):
            problems.append(
                "partial: amount_safe_to_pay must be greater than 0 and less than "
                "requested_amount"
            )
        if len(plan) != 2:
            problems.append(
                f"partial: payment_plan must contain exactly two payments, got {len(plan)}"
            )
        else:
            (d1, a1), (d2, a2) = plan
            if d1 != request.request_date or not _eq(a1, safe):
                problems.append(
                    "partial: the first payment must be amount_safe_to_pay on request_date"
                )
            if earliest is None:
                problems.append("partial: earliest_date_for_full_payment must not be empty")
            elif d2 != earliest:
                problems.append(
                    "partial: the second payment must fall on earliest_date_for_full_payment"
                )
            if not _eq(a2, requested - safe):
                problems.append(
                    "partial: the second payment must be requested_amount - amount_safe_to_pay"
                )
            if not _eq(a1 + a2, requested):
                problems.append("partial: the two payments must add up to requested_amount")
        if earliest is not None and deadline is not None and earliest > deadline:
            problems.append(
                "partial: earliest_date_for_full_payment must be on or before "
                "desired_completion_date (PS:146)"
            )
        if changes:
            problems.append("partial: must not require spending changes (PS:146)")

    # --- installments -------------------------------------------------------
    if method == "installments":
        if status != "affordable_with_plan":
            problems.append(
                f"installments: affordability_status must be affordable_with_plan, "
                f"got {status!r}"
            )
        if not plan:
            problems.append("installments: payment_plan must contain at least one payment")
        option_id = getattr(decision.chosen_plan, "option_id", None) if decision.chosen_plan else None
        if option_id is None:
            problems.append(
                "installments: the chosen plan must name the payment_option_id it matches"
            )

    # --- not_recommended / not_affordable -----------------------------------
    if status == "not_affordable" or method == "not_recommended":
        if plan:
            problems.append("not_affordable: payment_plan must be none")
        if changes:
            problems.append("not_affordable: spending_changes_needed must be none")
    if status == "not_affordable" and earliest is not None:
        problems.append("not_affordable: earliest_date_for_full_payment must be empty")

    # D14: affordable_later + not_recommended still has to report the capacity date.
    if status == "affordable_later" and method == "not_recommended":
        if earliest is None:
            problems.append(
                "affordable_later: earliest_date_for_full_payment must not be empty "
                "when the full amount becomes safe within the horizon"
            )
        elif earliest < request.request_date:
            # D27: the full amount may already be safe today while the user rejects
            # full_payment and nothing else is eligible, so `>= request_date` is
            # allowed here. `affordable_later + wait` still requires strictly after.
            problems.append(
                "affordable_later: earliest_date_for_full_payment must not be before "
                "request_date"
            )

    # --- spending changes ---------------------------------------------------
    if len(changes) > MAX_CHANGES:
        problems.append(
            f"changes: at most {MAX_CHANGES} spending changes allowed, got {len(changes)}"
        )
    by_event: dict[str, set[str]] = {}
    for change in changes:
        by_event.setdefault(change.event_id, set()).add(change.kind)
    for event_id, kinds in by_event.items():
        if len(kinds) > 1:
            problems.append(
                f"changes: stop and reduce_to must not both target {event_id!r} (PS:198)"
            )
    if len(by_event) != len(changes):
        problems.append("changes: the same event_id is targeted by more than one change")
    for change in changes:
        if change.kind == "stop" and change.new_amount is not None:
            problems.append(
                f"changes: stop:{change.event_id} must not carry a new amount"
            )
        if change.kind == "reduce_to" and (
            change.new_amount is None or float(change.new_amount) <= 0
        ):
            problems.append(
                f"changes: reduce_to:{change.event_id} needs a positive new amount"
            )
        if change.kind not in {"stop", "reduce_to"}:
            problems.append(f"changes: unknown change kind {change.kind!r}")
    if changes and not (status == "affordable_with_plan" and method in CHANGE_METHODS):
        problems.append(
            "changes: spending changes are only allowed for affordable_with_plan with "
            f"{sorted(CHANGE_METHODS)}, got {status!r}/{method!r}"
        )

    # --- explanation --------------------------------------------------------
    explanation = decision.decision_explanation or ""
    if not explanation.strip():
        problems.append("explanation: must not be empty")
    if "\n" in explanation or "\r" in explanation:
        problems.append("explanation: must not contain a newline")
    if len(explanation) > MAX_EXPLANATION:
        problems.append(
            f"explanation: {len(explanation)} chars, at most {MAX_EXPLANATION} allowed"
        )
    for _, amount in plan:
        if not any(s in explanation for s in _amount_strings(amount)):
            problems.append(
                f"explanation: does not mention the planned amount {fmt_amount(amount)}"
            )

    seen: set[str] = set()
    unique: list[str] = []
    for p in problems:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique
