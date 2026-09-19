"""One failing fixture per coherence rule in CONTRACT Section 2."""
from __future__ import annotations

import pathlib
import sys
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures" / "validate"))

import builders  # noqa: E402
from buyorwait.types import SpendingChange  # noqa: E402
from buyorwait.validate.coherence import check_coherence  # noqa: E402

REQ = builders.make_request
PROF = builders.make_profile


def problems(decision, request=None, profile=None):
    return check_coherence(decision, request or REQ(), profile if profile is not None else PROF())


def has(ps, needle):
    return any(needle in p for p in ps), ps


# --- amount_safe_to_pay ------------------------------------------------------

def test_safe_amount_above_requested_is_caught():
    d = builders.decision_full_payment()
    d.amount_safe_to_pay = builders.REQUESTED + 1
    ok, ps = has(problems(d), "exceeds requested_amount")
    assert ok, ps


def test_negative_safe_amount_is_caught():
    d = builders.decision_wait()
    d.amount_safe_to_pay = -1.0
    ok, ps = has(problems(d), "must be >= 0")
    assert ok, ps


# --- affordable_now ----------------------------------------------------------

def test_affordable_now_requires_earliest_equal_to_request_date():
    d = builders.decision_full_payment()
    d.earliest_date_for_full_payment = builders.REQUEST_DATE + timedelta(days=3)
    ok, ps = has(problems(d), "earliest_date_for_full_payment must equal request_date")
    assert ok, ps


def test_affordable_now_must_not_carry_spending_changes():
    d = builders.decision_full_payment()
    d.spending_changes_needed = [
        SpendingChange("stop", "event_12", None, "user_900:subscription:0")
    ]
    ok, ps = has(problems(d), "affordable_now: must not require spending changes")
    assert ok, ps


def test_affordable_now_plan_must_be_the_full_amount_on_request_date():
    d = builders.decision_full_payment()
    d.payment_plan = [(builders.REQUEST_DATE, 900.0)]
    ok, ps = has(
        problems(d), "affordable_now: payment_plan must be exactly the full amount"
    )
    assert ok, ps


def test_full_payment_with_changes_must_be_affordable_with_plan():
    d = builders.decision_full_payment_with_changes()
    d.affordability_status = "affordable_now"
    ok, ps = has(problems(d), "the status must be 'affordable_with_plan'")
    assert ok, ps


# --- status <-> method -------------------------------------------------------

def test_affordable_later_rejects_an_immediate_payment_method():
    d = builders.decision_wait()
    d.recommended_payment_method = "full_payment"
    ok, ps = has(problems(d), "status/method: affordable_later requires one of")
    assert ok, ps


def test_not_recommended_rejects_a_status_other_than_not_affordable_or_affordable_later():
    d = builders.decision_not_recommended()
    d.affordability_status = "affordable_with_plan"
    ok, ps = has(problems(d), "status/method: not_recommended requires one of")
    assert ok, ps


# --- D14: affordable_later + not_recommended --------------------------------

def _affordable_later_not_recommended():
    """Full payment becomes safe inside the horizon, but the user does not accept
    full_payment (so `wait` is ineligible, PS:189) and no other plan is safe."""
    d = builders.decision_not_recommended()
    d.affordability_status = "affordable_later"
    d.earliest_date_for_full_payment = builders.EARLIEST
    d.decision_explanation = (
        "Do not proceed now. The full INR 1,000 becomes safe on 10 February 2026, but "
        "none of the payment methods you accept completes it safely before then."
    )
    return d


@pytest.mark.parametrize(
    "earliest",
    [builders.EARLIEST, builders.REQUEST_DATE],  # D27: today is allowed too
    ids=["future_earliest", "earliest_is_request_date"],
)
def test_affordable_later_with_not_recommended_accepts_earliest_on_or_after_today(earliest):
    d = _affordable_later_not_recommended()
    d.earliest_date_for_full_payment = earliest
    profile = PROF(methods={"partial_payment", "installments"})
    assert problems(d, profile=profile) == []
    assert d.payment_plan == []
    assert d.spending_changes_needed == []
    assert d.earliest_date_for_full_payment >= builders.REQUEST_DATE


def test_affordable_later_with_not_recommended_rejects_an_earliest_before_request_date():
    d = _affordable_later_not_recommended()
    d.earliest_date_for_full_payment = builders.REQUEST_DATE - timedelta(days=1)
    ok, ps = has(
        problems(d, profile=PROF(methods={"partial_payment", "installments"})),
        "affordable_later: earliest_date_for_full_payment must not be before",
    )
    assert ok, ps


def test_affordable_later_with_not_recommended_requires_a_non_empty_earliest():
    d = _affordable_later_not_recommended()
    d.earliest_date_for_full_payment = None
    ok, ps = has(
        problems(d, profile=PROF(methods={"partial_payment", "installments"})),
        "affordable_later: earliest_date_for_full_payment must not be empty",
    )
    assert ok, ps


def test_method_must_be_one_the_user_accepts():
    d = builders.decision_installments()
    profile = PROF(methods={"full_payment"})
    ok, ps = has(problems(d, profile=profile), "is not in the user's accepted payment methods")
    assert ok, ps


# --- wait --------------------------------------------------------------------

def test_wait_requires_earliest_after_request_date():
    d = builders.decision_wait()
    d.earliest_date_for_full_payment = builders.REQUEST_DATE
    d.payment_plan = [(builders.REQUEST_DATE, builders.REQUESTED)]
    ok, ps = has(problems(d), "wait: earliest_date_for_full_payment must be after request_date")
    assert ok, ps


def test_wait_plan_must_be_the_full_amount_on_earliest():
    d = builders.decision_wait()
    d.payment_plan = [(builders.EARLIEST, builders.REQUESTED - 100)]
    ok, ps = has(problems(d), "wait: payment_plan must be exactly the full amount")
    assert ok, ps


# --- partial_payment ---------------------------------------------------------

def test_partial_must_have_exactly_two_payments():
    d = builders.decision_partial_payment()
    d.payment_plan = [(builders.REQUEST_DATE, builders.REQUESTED)]
    ok, ps = has(problems(d), "partial: payment_plan must contain exactly two payments")
    assert ok, ps


def test_partial_payments_must_sum_to_the_requested_amount():
    d = builders.decision_partial_payment()
    d.payment_plan = [(builders.REQUEST_DATE, 400.0), (builders.EARLIEST, 500.0)]
    ok, ps = has(problems(d), "partial: the two payments must add up to requested_amount")
    assert ok, ps


def test_partial_first_payment_must_be_safe_amount_on_request_date():
    d = builders.decision_partial_payment()
    d.payment_plan = [(builders.REQUEST_DATE, 300.0), (builders.EARLIEST, 700.0)]
    ok, ps = has(problems(d), "partial: the first payment must be amount_safe_to_pay")
    assert ok, ps


def test_partial_second_payment_must_fall_on_earliest():
    d = builders.decision_partial_payment()
    d.payment_plan = [
        (builders.REQUEST_DATE, builders.SAFE),
        (builders.EARLIEST + timedelta(days=1), builders.REQUESTED - builders.SAFE),
    ]
    ok, ps = has(problems(d), "partial: the second payment must fall on earliest")
    assert ok, ps


def test_partial_earliest_must_be_on_or_before_the_deadline():
    d = builders.decision_partial_payment()
    late = builders.DEADLINE + timedelta(days=5)
    d.earliest_date_for_full_payment = late
    d.payment_plan = [
        (builders.REQUEST_DATE, builders.SAFE),
        (late, builders.REQUESTED - builders.SAFE),
    ]
    ok, ps = has(problems(d), "on or before desired_completion_date")
    assert ok, ps


def test_partial_requires_the_request_to_allow_partial_payment():
    d = builders.decision_partial_payment()
    ok, ps = has(
        problems(d, request=REQ(allows_partial_payment=False)),
        "partial: the request does not allow partial payment",
    )
    assert ok, ps


# --- installments ------------------------------------------------------------

def test_installments_must_be_affordable_with_plan():
    d = builders.decision_installments()
    d.affordability_status = "affordable_later"
    ok, ps = has(problems(d), "installments: affordability_status must be affordable_with_plan")
    assert ok, ps


def test_installments_must_name_the_payment_option_they_match():
    d = builders.decision_installments()
    d.chosen_plan.option_id = None
    ok, ps = has(problems(d), "installments: the chosen plan must name the payment_option_id")
    assert ok, ps


# --- not_affordable ----------------------------------------------------------

def test_not_affordable_must_have_no_plan_no_changes_and_no_earliest():
    d = builders.decision_not_recommended()
    d.payment_plan = [(builders.REQUEST_DATE, 100.0)]
    d.earliest_date_for_full_payment = builders.EARLIEST
    d.spending_changes_needed = [
        SpendingChange("stop", "event_12", None, "user_900:subscription:0")
    ]
    ps = problems(d)
    assert any("not_affordable: payment_plan must be none" in p for p in ps), ps
    assert any("not_affordable: spending_changes_needed must be none" in p for p in ps), ps
    assert any("not_affordable: earliest_date_for_full_payment must be empty" in p for p in ps), ps


# --- spending changes --------------------------------------------------------

def test_more_than_three_spending_changes_is_caught():
    d = builders.decision_full_payment_with_changes()
    d.spending_changes_needed = [
        SpendingChange("stop", f"event_{i}", None, f"s:{i}") for i in (9, 12, 105, 7)
    ]
    ok, ps = has(problems(d), "at most 3 spending changes allowed")
    assert ok, ps


def test_stop_and_reduce_on_the_same_event_is_caught():
    d = builders.decision_full_payment_with_changes()
    d.spending_changes_needed = [
        SpendingChange("stop", "event_12", None, "s:1"),
        SpendingChange("reduce_to", "event_12", 50.0, "s:1"),
    ]
    ok, ps = has(problems(d), "stop and reduce_to must not both target")
    assert ok, ps


def test_spending_changes_are_not_allowed_on_a_wait_plan():
    d = builders.decision_wait()
    d.spending_changes_needed = [
        SpendingChange("stop", "event_12", None, "user_900:subscription:0")
    ]
    ok, ps = has(problems(d), "wait: must not require spending changes")
    assert ok, ps


def test_reduce_to_needs_a_positive_new_amount():
    d = builders.decision_full_payment_with_changes()
    d.spending_changes_needed = [SpendingChange("reduce_to", "event_12", None, "s:1")]
    ok, ps = has(problems(d), "needs a positive new amount")
    assert ok, ps


# --- plan shape and explanation ---------------------------------------------

def test_out_of_order_payments_are_caught():
    d = builders.decision_installments()
    d.payment_plan = list(reversed(d.payment_plan))
    ok, ps = has(problems(d), "plan: payments are not in chronological order")
    assert ok, ps


def test_explanation_must_mention_every_planned_amount():
    d = builders.decision_installments()
    d.decision_explanation = "Use 3 installments, starting 12 January 2026."
    ok, ps = has(problems(d), "does not mention the planned amount 350")
    assert ok, ps


def test_explanation_accepts_grouped_or_plain_amounts():
    grouped = builders.decision_full_payment()
    assert not any("does not mention" in p for p in problems(grouped))
    plain = builders.decision_full_payment()
    plain.decision_explanation = (
        "Pay INR 1000 today. This leaves at least INR 5,000 available over the next 90 days."
    )
    assert not any("does not mention" in p for p in problems(plain))


def test_empty_explanation_is_caught():
    d = builders.decision_wait()
    d.decision_explanation = "   "
    ok, ps = has(problems(d), "explanation: must not be empty")
    assert ok, ps
