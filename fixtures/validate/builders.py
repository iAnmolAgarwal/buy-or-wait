"""Hand-built Decision / Request / Profile fixtures for the validate tests.

Kept out of tests/ so that every test module can import the same scenario:

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "fixtures" / "validate"))
    import builders

Base scenario (deliberately boring so that each failing fixture isolates one rule):
  request_11x : INR 1,000 requested on 2026-01-05, deadline 2026-03-20, partial allowed
  profile     : accepts all three payment methods, keeps INR 5,000 minimum
"""
from __future__ import annotations

from datetime import date

from buyorwait.types import (
    Decision,
    EvidenceSet,
    PaymentOption,
    Plan,
    Profile,
    Request,
    SpendingChange,
)

REQUEST_DATE = date(2026, 1, 5)
DEADLINE = date(2026, 3, 20)
EARLIEST = date(2026, 2, 10)
REQUESTED = 1000.0
SAFE = 400.0
CCY = "INR"

EVENT_IDS = ["event_9", "event_12", "event_105"]
OPTION_IDS = ["option_1", "option_2"]


def make_request(**overrides) -> Request:
    kwargs = dict(
        request_id="request_900",
        user_id="user_900",
        request_date=REQUEST_DATE,
        request_type="purchase",
        requested_amount=REQUESTED,
        desired_completion_date=DEADLINE,
        allows_partial_payment=True,
        request_text="Can I afford the INR 1,000 purchase?",
    )
    kwargs.update(overrides)
    return Request(**kwargs)


def make_profile(**overrides) -> Profile:
    kwargs = dict(
        user_id="user_900",
        home_currency=CCY,
        current_available_balance=9000.0,
        minimum_balance_to_keep=5000.0,
        financial_priorities=["rent", "utilities"],
        protect_categories=["rent"],
        reduce_categories=["dining"],
        stop_categories=["subscription"],
        methods={"full_payment", "partial_payment", "installments"},
        max_installment_months=6,
    )
    kwargs.update(overrides)
    return Profile(**kwargs)


def make_option(**overrides) -> PaymentOption:
    kwargs = dict(
        payment_option_id="option_1",
        request_id="request_900",
        payment_method="installments",
        payment_amount=350.0,
        number_of_payments=3,
        first_payment_date=date(2026, 1, 12),
        payment_frequency_days=30,
        financing_fee=50.0,
        total_payable_amount=1050.0,
    )
    kwargs.update(overrides)
    return PaymentOption(**kwargs)


def make_evidence(**overrides) -> EvidenceSet:
    kwargs = dict(
        request_id="request_900",
        event_ids=list(EVENT_IDS),
        stream_ids=["user_900:dining:0", "user_900:subscription:0"],
        message_ids=["message_4"],
        image_ids=["image_2"],
        option_ids=list(OPTION_IDS),
    )
    kwargs.update(overrides)
    return EvidenceSet(**kwargs)


def _decision(**overrides) -> Decision:
    kwargs = dict(
        request_id="request_900",
        amount_safe_to_pay=SAFE,
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan=[],
        earliest_date_for_full_payment=None,
        spending_changes_needed=[],
        decision_explanation="",
        evidence=make_evidence(),
        chosen_plan=None,
        notes=[],
    )
    kwargs.update(overrides)
    return Decision(**kwargs)


# ---------------------------------------------------------------------------
# One valid Decision per recommended_payment_method
# ---------------------------------------------------------------------------

def decision_full_payment() -> Decision:
    """affordable_now + full_payment, no spending changes."""
    payments = [(REQUEST_DATE, REQUESTED)]
    return _decision(
        amount_safe_to_pay=REQUESTED,
        affordability_status="affordable_now",
        recommended_payment_method="full_payment",
        payment_plan=payments,
        earliest_date_for_full_payment=REQUEST_DATE,
        decision_explanation=(
            "Pay INR 1,000 today. This leaves at least INR 5,000 available over the "
            "next 90 days."
        ),
        chosen_plan=Plan(
            method="full_payment",
            payments=payments,
            changes=[],
            option_id=None,
            total_paid=REQUESTED,
            completes_by_deadline=True,
            min_balance_after=5200.0,
        ),
    )


def decision_full_payment_with_changes() -> Decision:
    """affordable_with_plan + full_payment made safe by one spending change."""
    payments = [(REQUEST_DATE, REQUESTED)]
    changes = [
        SpendingChange(
            kind="stop",
            event_id="event_12",
            new_amount=None,
            stream_id="user_900:subscription:0",
        )
    ]
    return _decision(
        amount_safe_to_pay=900.0,
        affordability_status="affordable_with_plan",
        recommended_payment_method="full_payment",
        payment_plan=payments,
        earliest_date_for_full_payment=EARLIEST,
        spending_changes_needed=changes,
        decision_explanation=(
            "Stop the family streaming plan, then pay INR 1,000 today. This leaves at "
            "least INR 5,000 available."
        ),
        chosen_plan=Plan(
            method="full_payment",
            payments=payments,
            changes=changes,
            option_id=None,
            total_paid=REQUESTED,
            completes_by_deadline=True,
            min_balance_after=5050.0,
        ),
    )


def decision_installments() -> Decision:
    """affordable_with_plan + installments matching option_1 exactly."""
    payments = make_option().schedule()
    return _decision(
        affordability_status="affordable_with_plan",
        recommended_payment_method="installments",
        payment_plan=payments,
        earliest_date_for_full_payment=EARLIEST,
        decision_explanation=(
            "Use 3 installments of INR 350, starting 12 January 2026. This leaves at "
            "least INR 5,000 available."
        ),
        chosen_plan=Plan(
            method="installments",
            payments=payments,
            changes=[],
            option_id="option_1",
            total_paid=1050.0,
            completes_by_deadline=True,
            min_balance_after=5300.0,
        ),
    )


def decision_partial_payment() -> Decision:
    """affordable_with_plan + partial_payment: safe today, remainder on earliest."""
    payments = [(REQUEST_DATE, SAFE), (EARLIEST, REQUESTED - SAFE)]
    return _decision(
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan=payments,
        earliest_date_for_full_payment=EARLIEST,
        decision_explanation=(
            "Pay INR 400 today and the remaining INR 600 on 10 February 2026. This "
            "completes the full request and keeps the INR 5,000 minimum protected."
        ),
        chosen_plan=Plan(
            method="partial_payment",
            payments=payments,
            changes=[],
            option_id=None,
            total_paid=REQUESTED,
            completes_by_deadline=True,
            min_balance_after=5000.0,
        ),
    )


def decision_wait() -> Decision:
    """affordable_later + wait: the whole amount, later."""
    payments = [(EARLIEST, REQUESTED)]
    return _decision(
        affordability_status="affordable_later",
        recommended_payment_method="wait",
        payment_plan=payments,
        earliest_date_for_full_payment=EARLIEST,
        decision_explanation=(
            "Pay INR 1,000 in full on 10 February 2026. Paying earlier would take the "
            "balance below the INR 5,000 minimum."
        ),
        chosen_plan=Plan(
            method="wait",
            payments=payments,
            changes=[],
            option_id=None,
            total_paid=REQUESTED,
            completes_by_deadline=True,
            min_balance_after=5000.0,
        ),
    )


def decision_not_recommended() -> Decision:
    """not_affordable + not_recommended: the only refusal shape."""
    return _decision(
        decision_explanation=(
            "Do not proceed with the INR 1,000 request. Although INR 400 is available "
            "today, the full amount cannot be completed safely within 90 days."
        ),
    )


VALID_DECISIONS = {
    "full_payment": decision_full_payment,
    "full_payment_with_changes": decision_full_payment_with_changes,
    "installments": decision_installments,
    "partial_payment": decision_partial_payment,
    "wait": decision_wait,
    "not_recommended": decision_not_recommended,
}
