"""decide(): status / method / plan coherence and explanation hygiene.

The assertions in `assert_coherent` are the CONTRACT Section 2
validate/coherence rules, checked here against randomly generated profiles so
the engine can never emit a row the validator would reject.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from buyorwait.types import (
    CashFlow,
    EvidenceSet,
    PaymentOption,
    Profile,
    RecurringStream,
    Request,
)
from buyorwait.engine.decide import decide
from buyorwait.engine.explain import explain
from buyorwait.engine.plans import MAX_CHANGES
from buyorwait.engine.policy import Policy
from buyorwait.engine.solver import amount_safe_to_pay, earliest_full_payment_date
from buyorwait.formatting import fmt_money

POLICY = Policy()
START = date(2026, 3, 1)
ALL_METHODS = ["full_payment", "partial_payment", "installments"]
FLEX = ["fixed", "reducible", "stoppable", "reducible_or_stoppable"]
CATS = ["dining", "subscription", "groceries", "rent"]

EVIDENCE = EvidenceSet("request_26", [], [], [], [], [])


def _stream(i, category, amount, flexibility, direction="debit"):
    return RecurringStream(
        stream_id=f"user_01:{category}:{i}",
        category=category,
        # no digits in the description: the explanation-hygiene test scans for numbers
        description=f"{category} item {'abcdef'[i]}",
        direction=direction,
        cadence_days=30,
        monthly=True,
        amount=amount,
        anchor_event_id=f"event_{100 + i}",
        last_date=date(2026, 2, 10),
        flexibility=flexibility,
        minimum_allowed_amount=round(amount / 2, 2),
        event_ids=[f"event_{100 + i}"],
    )


def _flows(streams):
    from buyorwait.engine.forecast import _occurrences

    out = []
    for s in streams:
        for d in _occurrences(s, START, START + timedelta(days=POLICY.horizon_days)):
            out.append(
                CashFlow(
                    flow_date=d,
                    amount=(s.amount if s.direction == "credit" else -s.amount),
                    source_id=s.stream_id,
                    kind="recurring",
                    category=s.category,
                    label=s.description,
                    flexibility=s.flexibility,
                    minimum_allowed_amount=s.minimum_allowed_amount,
                    stream_id=s.stream_id,
                )
            )
    return out


money = st.integers(1, 2_000_00).map(lambda c: c / 100.0)


@st.composite
def world(draw):
    streams = [
        _stream(
            i,
            draw(st.sampled_from(CATS)),
            draw(money),
            draw(st.sampled_from(FLEX)),
            direction=draw(st.sampled_from(["debit", "debit", "credit"])),
        )
        for i in range(draw(st.integers(0, 4)))
    ]
    profile = Profile(
        user_id="user_01",
        home_currency=draw(st.sampled_from(["INR", "EUR", "ZAR", "IDR", "USD"])),
        current_available_balance=draw(st.integers(0, 30_000_00).map(lambda c: c / 100.0)),
        minimum_balance_to_keep=draw(st.integers(0, 5_000_00).map(lambda c: c / 100.0)),
        financial_priorities=[],
        protect_categories=[],
        reduce_categories=draw(st.lists(st.sampled_from(CATS), unique=True)),
        stop_categories=draw(st.lists(st.sampled_from(CATS), unique=True)),
        methods=set(draw(st.lists(st.sampled_from(ALL_METHODS), unique=True))),
        max_installment_months=draw(st.one_of(st.none(), st.integers(1, 12))),
    )
    requested = draw(st.integers(1, 6_000_00).map(lambda c: c / 100.0))
    request = Request(
        request_id="request_26",
        user_id="user_01",
        request_date=START,
        request_type="purchase",
        requested_amount=requested,
        desired_completion_date=START + timedelta(days=draw(st.integers(0, 120))),
        allows_partial_payment=draw(st.booleans()),
        request_text="",
    )
    options = []
    for j in range(draw(st.integers(1, 2))):
        n = draw(st.integers(1, 6))
        amt = round(requested / n, 2)
        options.append(
            PaymentOption(
                payment_option_id=f"payment_option_{j + 2:02d}",
                request_id="request_26",
                payment_method="installments",
                payment_amount=amt,
                number_of_payments=n,
                first_payment_date=START + timedelta(days=draw(st.integers(0, 20))),
                payment_frequency_days=draw(st.integers(7, 40)),
                financing_fee=0.0,
                total_payable_amount=round(amt * n, 2),
            )
        )
    return profile, request, options, streams, _flows(streams)


def assert_coherent(decision, request, profile, options, streams):
    d = decision
    requested = float(request.requested_amount)
    by_id = {s.stream_id: s for s in streams}

    # 0 <= safe <= requested  (PS:110)
    assert 0.0 <= d.amount_safe_to_pay <= requested
    assert d.affordability_status in {
        "affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"
    }
    assert d.recommended_payment_method in {
        "full_payment", "partial_payment", "installments", "wait", "not_recommended"
    }
    # payment_plan is chronological
    dates = [x[0] for x in d.payment_plan]
    assert dates == sorted(dates)

    if d.affordability_status == "affordable_now":
        assert d.recommended_payment_method == "full_payment"
        assert d.earliest_date_for_full_payment == request.request_date
        assert d.payment_plan == [(request.request_date, requested)]
        assert d.spending_changes_needed == []
        assert d.amount_safe_to_pay == requested

    if d.recommended_payment_method == "partial_payment":
        assert d.affordability_status == "affordable_with_plan"
        assert len(d.payment_plan) == 2
        assert sum(a for _, a in d.payment_plan) == pytest.approx(requested, abs=0.01)
        assert d.payment_plan[0] == (request.request_date, d.amount_safe_to_pay)
        assert d.payment_plan[1][0] == d.earliest_date_for_full_payment
        assert d.earliest_date_for_full_payment <= request.desired_completion_date
        assert request.allows_partial_payment
        assert d.spending_changes_needed == []

    if d.recommended_payment_method == "installments":
        assert d.affordability_status == "affordable_with_plan"
        matching = [o for o in options if o.payment_option_id == d.chosen_plan.option_id]
        assert len(matching) == 1
        assert d.payment_plan == matching[0].schedule()

    if d.recommended_payment_method == "wait":
        assert d.affordability_status == "affordable_later"
        assert d.payment_plan == [(d.earliest_date_for_full_payment, requested)]
        assert d.earliest_date_for_full_payment > request.request_date
        assert d.spending_changes_needed == []

    if d.affordability_status == "not_affordable":
        assert d.recommended_payment_method == "not_recommended"
        assert d.payment_plan == []
        assert d.spending_changes_needed == []
        assert d.chosen_plan is None
        # D14: not_affordable only when the full amount never becomes safe
        assert d.earliest_date_for_full_payment is None

    if d.recommended_payment_method == "not_recommended":
        assert d.payment_plan == []
        assert d.spending_changes_needed == []
        assert d.chosen_plan is None
        # D14: no eligible plan but the money does arrive -> affordable_later
        if d.earliest_date_for_full_payment is not None:
            assert d.affordability_status == "affordable_later"
        else:
            assert d.affordability_status == "not_affordable"

    if d.recommended_payment_method == "full_payment":
        assert d.payment_plan == [(request.request_date, requested)]
        if d.spending_changes_needed:
            assert d.affordability_status == "affordable_with_plan"

    # spending changes: <= 3, never two on the same event, always permitted
    chs = d.spending_changes_needed
    assert len(chs) <= MAX_CHANGES
    assert len({c.event_id for c in chs}) == len(chs)
    assert len({c.stream_id for c in chs}) == len(chs)
    for c in chs:
        s = by_id[c.stream_id]
        assert s.direction == "debit"
        assert c.event_id == s.anchor_event_id
        if c.kind == "stop":
            assert s.flexibility in ("stoppable", "reducible_or_stoppable")
            assert s.category in profile.stop_categories
        else:
            assert c.kind == "reduce_to"
            assert s.flexibility in ("reducible", "reducible_or_stoppable")
            assert s.category in profile.reduce_categories
            assert c.new_amount == s.minimum_allowed_amount

    # the explanation names every amount in the plan, and every changed stream
    ccy = profile.home_currency
    for _, a in d.payment_plan:
        assert fmt_money(ccy, a) in d.decision_explanation
    for c in chs:
        assert by_id[c.stream_id].description.lower() in d.decision_explanation
    # ... and never names a stream it did not change
    changed = {c.stream_id for c in chs}
    for s in streams:
        if s.stream_id not in changed:
            assert s.description.lower() not in d.decision_explanation

    assert d.notes  # decide always logs why


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(world())
def test_decide_is_always_coherent(w):
    profile, request, options, streams, flows = w
    d = decide(request, profile, options, streams, flows, EVIDENCE, POLICY)
    assert_coherent(d, request, profile, options, streams)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(world())
def test_decide_amount_safe_and_earliest_match_the_solver(w):
    """The two preference-independent columns are the solver's, untouched (PS:163)."""
    profile, request, options, streams, flows = w
    d = decide(request, profile, options, streams, flows, EVIDENCE, POLICY)
    assert d.amount_safe_to_pay == amount_safe_to_pay(profile, flows, request, POLICY)
    earliest = earliest_full_payment_date(profile, flows, request, POLICY)
    assert d.earliest_date_for_full_payment == earliest


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(world())
def test_explanation_contains_no_invented_numbers(w):
    profile, request, options, streams, flows = w
    d = decide(request, profile, options, streams, flows, EVIDENCE, POLICY)

    allowed: set[str] = {"90"}

    def add(x: float) -> None:
        allowed.add(fmt_money("X", x).split(" ", 1)[1].replace(",", ""))

    add(float(request.requested_amount))
    add(d.amount_safe_to_pay)
    add(float(profile.minimum_balance_to_keep))
    for _, a in d.payment_plan:
        add(a)
    for c in d.spending_changes_needed:
        add(float(c.new_amount or 0.0))
    allowed.add(str(len(d.payment_plan)))
    dates = [request.desired_completion_date] + [x[0] for x in d.payment_plan]
    if d.earliest_date_for_full_payment is not None:
        dates.append(d.earliest_date_for_full_payment)
    for dt in dates:
        allowed.add(str(dt.day))
        allowed.add(str(dt.year))

    tokens = re.findall(r"\d[\d,]*(?:\.\d+)?", d.decision_explanation)
    for t in tokens:
        assert t.replace(",", "") in allowed, (t, d.decision_explanation, sorted(allowed))


# ---------------------------------------------------------------------------
# Explanation templates (CONTRACT Section 4)
# ---------------------------------------------------------------------------


def _profile(**kw) -> Profile:
    base = dict(
        user_id="user_01", home_currency="EUR", current_available_balance=5000.0,
        minimum_balance_to_keep=800.0, financial_priorities=[], protect_categories=[],
        reduce_categories=["dining"], stop_categories=["subscription"],
        methods=set(ALL_METHODS), max_installment_months=6,
    )
    base.update(kw)
    return Profile(**base)


def _request(**kw) -> Request:
    base = dict(
        request_id="request_06", user_id="user_01", request_date=date(2026, 1, 3),
        request_type="investment", requested_amount=620.4,
        desired_completion_date=date(2026, 1, 14), allows_partial_payment=False, request_text="",
    )
    base.update(kw)
    return Request(**base)


def _plan(**kw):
    from buyorwait.types import Plan

    base = dict(method="full_payment", payments=[(date(2026, 1, 3), 620.4)], changes=[],
                option_id=None, total_paid=620.4, completes_by_deadline=True, min_balance_after=900.0)
    base.update(kw)
    return Plan(**base)


def test_template_affordable_now():
    txt = explain(_request(), _profile(), _plan(), "affordable_now", 620.4)
    assert txt == "Pay EUR 620.40 today. This leaves at least EUR 800 available over the next 90 days."


def test_template_installments():
    p = _plan(method="installments", option_id="payment_option_02",
              payments=[(date(2026, 1, 5), 206.80)] * 3, total_paid=620.4)
    txt = explain(_request(), _profile(), p, "affordable_with_plan", 100.0)
    assert txt == (
        "Use 3 installments of EUR 206.80, starting 5 January 2026. "
        "This leaves at least EUR 800 available."
    )


def test_template_partial():
    p = _plan(method="partial_payment",
              payments=[(date(2026, 1, 3), 400.0), (date(2026, 1, 12), 220.4)])
    txt = explain(_request(), _profile(), p, "affordable_with_plan", 400.0)
    assert txt == (
        "Pay EUR 400 today and the remaining EUR 220.40 on 12 January 2026. "
        "This completes the full request and keeps the EUR 800 minimum protected."
    )


def test_template_wait():
    p = _plan(method="wait", payments=[(date(2026, 1, 15), 620.4)], completes_by_deadline=False)
    txt = explain(_request(), _profile(), p, "affordable_later", 100.0)
    assert txt == (
        "Pay EUR 620.40 in full on 15 January 2026. "
        "Paying earlier would take the balance below the EUR 800 minimum."
    )


def test_template_with_changes_uses_stream_descriptions():
    from buyorwait.types import SpendingChange

    streams = [
        _stream(0, "subscription", 30.0, "stoppable"),
        _stream(1, "dining", 100.0, "reducible"),
    ]
    streams[0].description = "Online backup subscription"
    streams[1].description = "Streaming subscription"
    changes = [
        SpendingChange("stop", "event_100", None, streams[0].stream_id),
        SpendingChange("reduce_to", "event_101", 23.5, streams[1].stream_id),
    ]
    p = _plan(changes=changes)
    txt = explain(_request(), _profile(), p, "affordable_with_plan", 600.0, streams)
    assert txt == (
        "Stop the online backup subscription and reduce the streaming subscription to EUR 23.50, "
        "then pay EUR 620.40 today. This leaves at least EUR 800 available."
    )


def test_d14_no_eligible_plan_but_money_arrives_is_affordable_later():
    """The user accepts only installments, there is no installment option, and the
    full amount becomes safe on the salary date. PS:122 -- that is not
    'cannot be completed safely within the forecast period'."""
    salary = _stream(0, "rent", 0.0, "fixed")
    salary.category = "salary"
    salary.description = "monthly salary"
    salary.direction = "credit"
    salary.amount = 4000.0
    salary.last_date = date(2026, 2, 20)
    flows = _flows([salary])
    profile = _profile(
        home_currency="INR", current_available_balance=1000.0, minimum_balance_to_keep=800.0,
        methods={"installments"},
    )
    request = _request(request_date=START, requested_amount=2000.0,
                       desired_completion_date=START + timedelta(days=90))
    d = decide(request, profile, [], [salary], flows, EVIDENCE, POLICY)

    assert d.affordability_status == "affordable_later"
    assert d.recommended_payment_method == "not_recommended"
    assert d.payment_plan == []
    assert d.spending_changes_needed == []
    assert d.chosen_plan is None
    assert d.earliest_date_for_full_payment == date(2026, 3, 20)
    assert d.decision_explanation == (
        "Do not proceed with the INR 2,000 request using the accepted methods. "
        "The full amount is forecast to be safe as a single payment from 20 March 2026."
    )
    assert_coherent(d, request, profile, [], [salary])


def test_d27_earliest_equal_to_request_date_with_no_eligible_plan():
    """Full payment is safe today over the whole 90 days, but the user accepts
    only installments and there is no option. D14 status, D27 date: the row is
    affordable_later / not_recommended with earliest == request_date, which the
    coherence gate accepts for this combination."""
    profile = _profile(home_currency="INR", current_available_balance=100000.0,
                       minimum_balance_to_keep=800.0, methods={"installments"})
    request = _request(request_date=START, requested_amount=2000.0,
                       desired_completion_date=START + timedelta(days=30))
    d = decide(request, profile, [], [], [], EVIDENCE, POLICY)

    assert d.earliest_date_for_full_payment == START
    assert d.amount_safe_to_pay == request.requested_amount     # D27 iff
    assert d.affordability_status == "affordable_later"
    assert d.recommended_payment_method == "not_recommended"
    assert d.payment_plan == []
    assert d.spending_changes_needed == []
    assert_coherent(d, request, profile, [], [])


def test_d14_not_affordable_still_applies_when_the_money_never_arrives():
    profile = _profile(home_currency="INR", current_available_balance=1000.0,
                       minimum_balance_to_keep=800.0, methods={"installments"})
    request = _request(request_date=START, requested_amount=50000.0,
                       desired_completion_date=START + timedelta(days=90))
    d = decide(request, profile, [], [], [], EVIDENCE, POLICY)
    assert d.affordability_status == "not_affordable"
    assert d.recommended_payment_method == "not_recommended"
    assert d.earliest_date_for_full_payment is None
    assert d.decision_explanation.startswith("Do not proceed with the INR 50,000 request. Although")


def test_template_affordable_later_without_a_plan():
    txt = explain(_request(), _profile(), None, "affordable_later", 100.0, (), date(2026, 2, 15))
    assert txt == (
        "Do not proceed with the EUR 620.40 request using the accepted methods. "
        "The full amount is forecast to be safe as a single payment from 15 February 2026."
    )


def test_template_not_affordable_both_variants():
    txt = explain(_request(), _profile(), None, "not_affordable", 597.74)
    assert txt == (
        "Do not proceed with the EUR 620.40 request. Although EUR 597.74 is available today, "
        "the full amount cannot be completed safely within 90 days."
    )
    txt = explain(_request(), _profile(), None, "not_affordable", 0.0)
    assert txt == (
        "Do not make this payment by 14 January 2026. "
        "None of the available options keeps the EUR 800 minimum protected."
    )
