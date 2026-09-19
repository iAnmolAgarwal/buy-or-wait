"""enumerate_plans / rank (CONTRACT engine/plans.py, PS:146, PS:191-198)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from buyorwait.types import CashFlow, PaymentOption, Plan, Profile, RecurringStream, Request, SpendingChange
from buyorwait.engine.plans import MAX_CHANGES, apply_changes, enumerate_plans, rank, rank_key
from buyorwait.engine.policy import Policy
from buyorwait.engine.solver import (
    amount_safe_to_pay,
    check_horizon_for,
    earliest_full_payment_date,
    min_balance,
)

POLICY = Policy()
START = date(2026, 3, 1)
ALL_METHODS = ["full_payment", "partial_payment", "installments"]


def mk_profile(**kw) -> Profile:
    base = dict(
        user_id="user_01",
        home_currency="INR",
        current_available_balance=100000.0,
        minimum_balance_to_keep=20000.0,
        financial_priorities=[],
        protect_categories=[],
        reduce_categories=["dining"],
        stop_categories=["subscription"],
        methods=set(ALL_METHODS),
        max_installment_months=6,
    )
    base.update(kw)
    return Profile(**base)


def mk_request(**kw) -> Request:
    base = dict(
        request_id="request_26",
        user_id="user_01",
        request_date=START,
        request_type="purchase",
        requested_amount=50000.0,
        desired_completion_date=START + timedelta(days=60),
        allows_partial_payment=True,
        request_text="",
    )
    base.update(kw)
    return Request(**base)


def mk_option(**kw) -> PaymentOption:
    base = dict(
        payment_option_id="payment_option_02",
        request_id="request_26",
        payment_method="installments",
        payment_amount=17000.0,
        number_of_payments=3,
        first_payment_date=START,
        payment_frequency_days=30,
        financing_fee=1000.0,
        total_payable_amount=51000.0,
    )
    base.update(kw)
    return PaymentOption(**base)


def mk_stream(**kw) -> RecurringStream:
    base = dict(
        stream_id="user_01:dining:0",
        category="dining",
        description="Weekend food delivery",
        direction="debit",
        cadence_days=30,
        monthly=True,
        amount=8000.0,
        anchor_event_id="event_101",
        last_date=date(2026, 2, 10),
        flexibility="reducible_or_stoppable",
        minimum_allowed_amount=2000.0,
        event_ids=["event_99", "event_100", "event_101"],
    )
    base.update(kw)
    return RecurringStream(**base)


def flows_for(streams, horizon=90):
    """The flows build_flows would produce for these streams (no amendments)."""
    from buyorwait.engine.forecast import _occurrences

    out = []
    for s in streams:
        for d in _occurrences(s, START, START + timedelta(days=horizon)):
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


def run(profile, request, options, flows, streams, notes=None):
    safe = amount_safe_to_pay(profile, flows, request, POLICY)
    earliest = earliest_full_payment_date(profile, flows, request, POLICY)
    plans = enumerate_plans(profile, request, options, flows, streams, safe, earliest, POLICY, notes=notes)
    return safe, earliest, plans


# ---------------------------------------------------------------------------
# (3) partial payment shape (PS:146)
# ---------------------------------------------------------------------------

money = st.integers(min_value=1, max_value=400_000).map(lambda c: c / 100.0)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    balance=money,
    minimum=money,
    requested=money,
    salary=money,
    rent=money,
    deadline_offset=st.integers(0, 120),
)
def test_partial_plans_have_exactly_two_payments_summing_to_requested(
    balance, minimum, requested, salary, rent, deadline_offset
):
    profile = mk_profile(current_available_balance=balance, minimum_balance_to_keep=minimum)
    request = mk_request(
        requested_amount=requested,
        allows_partial_payment=True,
        desired_completion_date=START + timedelta(days=deadline_offset),
    )
    streams = [
        mk_stream(stream_id="s:salary", category="salary", direction="credit", amount=salary,
                  description="Monthly salary", anchor_event_id="event_1", flexibility="fixed",
                  minimum_allowed_amount=None, last_date=date(2026, 2, 15)),
        mk_stream(stream_id="s:rent", category="rent", direction="debit", amount=rent,
                  description="Rent", anchor_event_id="event_2", flexibility="fixed",
                  minimum_allowed_amount=None, last_date=date(2026, 2, 5)),
    ]
    flows = flows_for(streams)
    safe, earliest, plans = run(profile, request, [], flows, streams)

    partials = [p for p in plans if p.method == "partial_payment"]
    for p in partials:
        assert len(p.payments) == 2
        assert sum(a for _, a in p.payments) == pytest.approx(requested, abs=0.01)
        assert p.payments[0] == (START, safe)
        assert p.payments[1][0] == earliest
        assert 0 < safe < requested
        assert earliest is not None and earliest <= request.desired_completion_date
        assert p.changes == []                    # PS:183 -- partial is defined without changes
        assert p.payments[0][0] < p.payments[1][0]


def test_partial_is_rejected_when_the_request_forbids_it():
    profile = mk_profile(current_available_balance=30000.0, minimum_balance_to_keep=20000.0)
    request = mk_request(requested_amount=50000.0, allows_partial_payment=False)
    streams = [mk_stream(stream_id="s:salary", category="salary", direction="credit", amount=60000.0,
                         description="Salary", anchor_event_id="event_1", flexibility="fixed",
                         minimum_allowed_amount=None, last_date=date(2026, 2, 15))]
    flows = flows_for(streams)
    notes: list[str] = []
    _, _, plans = run(profile, request, [], flows, streams, notes)
    assert not [p for p in plans if p.method == "partial_payment"]
    assert any("does not allow partial payment" in n for n in notes)


# ---------------------------------------------------------------------------
# (4) installment plans match an option schedule exactly
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(
    n=st.integers(1, 6),
    amount=st.integers(1, 500_000).map(lambda c: c / 100.0),
    freq=st.integers(1, 45),
    first_offset=st.integers(0, 40),
    balance=st.integers(0, 100_000_000).map(lambda c: c / 100.0),
)
def test_installment_plans_equal_the_option_schedule(n, amount, freq, first_offset, balance):
    profile = mk_profile(current_available_balance=balance, minimum_balance_to_keep=0.0,
                         max_installment_months=12)
    request = mk_request(requested_amount=amount * n,
                         desired_completion_date=START + timedelta(days=365))
    opt = mk_option(payment_amount=amount, number_of_payments=n, payment_frequency_days=freq,
                    first_payment_date=START + timedelta(days=first_offset),
                    total_payable_amount=round(amount * n, 2))
    _, _, plans = run(profile, request, [opt], [], [])
    inst = [p for p in plans if p.method == "installments"]
    for p in inst:
        assert p.option_id == opt.payment_option_id
        assert p.payments == opt.schedule()
        assert len(p.payments) == opt.number_of_payments
        assert p.total_paid == pytest.approx(opt.total_payable_amount)


def test_installments_capped_by_max_installment_months_and_by_the_deadline():
    profile = mk_profile(current_available_balance=10_000_000.0, minimum_balance_to_keep=0.0,
                         max_installment_months=3)
    request = mk_request(desired_completion_date=START + timedelta(days=70))
    too_many = mk_option(payment_option_id="payment_option_03", number_of_payments=6,
                         payment_amount=9000.0, total_payable_amount=54000.0)
    too_late = mk_option(payment_option_id="payment_option_04", number_of_payments=3,
                         payment_frequency_days=40, payment_amount=17000.0,
                         total_payable_amount=51000.0)
    ok = mk_option(payment_option_id="payment_option_02", number_of_payments=3,
                   payment_frequency_days=30, payment_amount=17000.0, total_payable_amount=51000.0)
    notes: list[str] = []
    _, _, plans = run(profile, request, [too_many, too_late, ok], [], [], notes)
    assert [p.option_id for p in plans if p.method == "installments"] == ["payment_option_02"]
    assert any("max_installment_months" in n for n in notes)
    assert any("after the desired completion date" in n for n in notes)


def test_payments_beyond_the_horizon_are_not_simulated():
    """A payment on day 200 cannot make the 90-day path unsafe."""
    profile = mk_profile(current_available_balance=40000.0, minimum_balance_to_keep=20000.0,
                         max_installment_months=12)
    request = mk_request(requested_amount=30000.0,
                         desired_completion_date=START + timedelta(days=365))
    opt = mk_option(payment_amount=15000.0, number_of_payments=2, payment_frequency_days=200,
                    total_payable_amount=30000.0)
    _, _, plans = run(profile, request, [opt], [], [])
    assert [p.option_id for p in plans if p.method == "installments"] == [opt.payment_option_id]


# ---------------------------------------------------------------------------
# (5) spending-change invariants (PS:198)
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    balance=st.integers(0, 20_000_00).map(lambda c: c / 100.0),
    minimum=st.integers(0, 10_000_00).map(lambda c: c / 100.0),
    requested=st.integers(1, 10_000_00).map(lambda c: c / 100.0),
    amounts=st.lists(st.integers(1000, 900_00).map(lambda c: c / 100.0), min_size=1, max_size=5),
    flex=st.lists(st.sampled_from(["fixed", "reducible", "stoppable", "reducible_or_stoppable"]),
                  min_size=1, max_size=5),
    reduce_ok=st.booleans(),
    stop_ok=st.booleans(),
)
def test_change_sets_are_small_valid_and_permitted(
    balance, minimum, requested, amounts, flex, reduce_ok, stop_ok
):
    streams = []
    for i, amt in enumerate(amounts):
        f = flex[i % len(flex)]
        streams.append(
            mk_stream(
                stream_id=f"s:{i}",
                category="dining" if i % 2 == 0 else "subscription",
                description=f"expense {i}",
                amount=amt,
                anchor_event_id=f"event_{100 + i}",
                flexibility=f,
                minimum_allowed_amount=round(amt / 2, 2),
                last_date=date(2026, 2, 10),
                monthly=True,
            )
        )
    profile = mk_profile(
        current_available_balance=balance,
        minimum_balance_to_keep=minimum,
        reduce_categories=["dining", "subscription"] if reduce_ok else [],
        stop_categories=["dining", "subscription"] if stop_ok else [],
    )
    request = mk_request(requested_amount=requested,
                         desired_completion_date=START + timedelta(days=60))
    flows = flows_for(streams)
    by_id = {s.stream_id: s for s in streams}
    opt = mk_option(payment_amount=round(requested / 3, 2), number_of_payments=3,
                    total_payable_amount=round(requested, 2))
    _, _, plans = run(profile, request, [opt], flows, streams)

    for p in plans:
        assert len(p.changes) <= MAX_CHANGES                          # PS:148-161
        sids = [c.stream_id for c in p.changes]
        assert len(sids) == len(set(sids))                            # PS:198 no stop+reduce on one event
        eids = [c.event_id for c in p.changes]
        assert len(eids) == len(set(eids))
        if p.changes:
            assert p.method in ("full_payment", "installments")       # PS:146/183
        for c in p.changes:
            s = by_id[c.stream_id]
            assert c.event_id == s.anchor_event_id                    # G6
            if c.kind == "stop":
                assert s.flexibility in ("stoppable", "reducible_or_stoppable")
                assert s.category in profile.stop_categories
                assert c.new_amount is None
            else:
                assert c.kind == "reduce_to"
                assert s.flexibility in ("reducible", "reducible_or_stoppable")
                assert s.category in profile.reduce_categories
                assert c.new_amount == s.minimum_allowed_amount       # G5
        # Every returned plan passes the safety check over its own window: the
        # full 90 days, except `wait`, which D25 judges over check_horizon_for.
        window = (
            check_horizon_for(request, p.payments[0][0], POLICY)
            if p.method == "wait"
            else POLICY.horizon_days
        )
        sim = [(d, a) for d, a in p.payments if d <= START + timedelta(days=window)]
        assert POLICY.meets_minimum(
            min_balance(profile, apply_changes(flows, p.changes), START, window, sim),
            minimum,
        )


def test_changes_are_only_used_when_the_plan_is_unsafe_without_them():
    streams = [mk_stream()]
    flows = flows_for(streams)
    rich = mk_profile(current_available_balance=10_000_000.0)
    _, _, plans = run(rich, mk_request(), [], flows, streams)
    full = [p for p in plans if p.method == "full_payment"][0]
    assert full.changes == []

    poor = mk_profile(current_available_balance=70000.0, minimum_balance_to_keep=20000.0)
    _, _, plans = run(poor, mk_request(requested_amount=30000.0), [], flows, streams)
    full = [p for p in plans if p.method == "full_payment"][0]
    assert full.changes and full.changes[0].event_id == "event_101"


def test_changes_apply_to_every_future_occurrence_of_the_stream():
    streams = [mk_stream()]
    flows = flows_for(streams)
    change = SpendingChange("reduce_to", "event_101", 2000.0, "user_01:dining:0")
    after = apply_changes(flows, [change])
    assert len(after) == len(flows) > 1
    assert all(f.amount == -2000.0 for f in after)
    stopped = apply_changes(flows, [SpendingChange("stop", "event_101", None, "user_01:dining:0")])
    assert stopped == []


# ---------------------------------------------------------------------------
# (6) ranking (PS:191-196)
# ---------------------------------------------------------------------------


def plan(method="full_payment", payments=None, changes=(), option_id=None, total=100.0,
         by_deadline=True, min_after=0.0) -> Plan:
    return Plan(
        method=method,
        payments=list(payments if payments is not None else [(START, 100.0)]),
        changes=list(changes),
        option_id=option_id,
        total_paid=total,
        completes_by_deadline=by_deadline,
        min_balance_after=min_after,
    )


CHANGE = SpendingChange("stop", "event_1", None, "s:1")


def test_rank_returns_none_for_no_plans():
    assert rank([]) is None


def test_rank_criterion_1_completes_by_deadline_wins():
    late = plan(by_deadline=False, total=10.0)
    ontime = plan(by_deadline=True, total=999.0, changes=[CHANGE])
    assert rank([late, ontime]) is ontime


def test_rank_criterion_2_no_changes_beats_cheaper_with_changes():
    clean = plan(total=200.0)
    cheap = plan(total=10.0, changes=[CHANGE])
    assert rank([cheap, clean]) is clean


def test_rank_criterion_3_lower_total_paid():
    a = plan(total=200.0)
    b = plan(total=150.0)
    assert rank([a, b]) is b


def test_rank_criterion_4_earlier_start():
    early = plan(payments=[(START, 100.0)])
    late = plan(payments=[(START + timedelta(days=5), 100.0)])
    assert rank([late, early]) is early


def test_rank_criterion_5_fewer_payments():
    one = plan(payments=[(START, 100.0)])
    three = plan(payments=[(START, 40.0), (START + timedelta(days=1), 30.0),
                           (START + timedelta(days=2), 30.0)], total=100.0)
    assert rank([three, one]) is one


def test_rank_criterion_6_lowest_option_id_is_the_final_tiebreak():
    a = plan(method="installments", option_id="payment_option_09")
    b = plan(method="installments", option_id="payment_option_04")
    assert rank([a, b]) is b


def test_rank_criteria_are_applied_strictly_in_order():
    """Each criterion only ever decides ties left by the ones before it."""
    plans = [
        plan(by_deadline=False, total=1.0, option_id="payment_option_01"),
        plan(total=500.0, changes=[CHANGE], option_id="payment_option_02"),
        plan(total=400.0, option_id="payment_option_07"),
        plan(total=400.0, payments=[(START - timedelta(days=1), 400.0)], option_id="payment_option_08"),
    ]
    best = rank(plans)
    assert best is plans[3]
    keys = [rank_key(p) for p in sorted(plans, key=rank_key)]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# wait (PS:189)
# ---------------------------------------------------------------------------


def test_wait_stays_eligible_past_the_deadline_when_the_deadline_is_hard():
    streams = [mk_stream(stream_id="s:salary", category="salary", direction="credit", amount=200000.0,
                         description="Salary", anchor_event_id="event_1", flexibility="fixed",
                         minimum_allowed_amount=None, last_date=date(2026, 2, 20))]
    flows = flows_for(streams)
    profile = mk_profile(current_available_balance=25000.0, minimum_balance_to_keep=20000.0,
                         methods={"full_payment"})
    request = mk_request(requested_amount=50000.0, desired_completion_date=START + timedelta(days=5))
    safe, earliest, plans = run(profile, request, [], flows, streams)
    waits = [p for p in plans if p.method == "wait"]
    assert waits and waits[0].payments == [(earliest, 50000.0)]
    assert earliest > request.desired_completion_date
    assert waits[0].completes_by_deadline is False
    assert rank(plans) is waits[0]


def test_d25_wait_uses_the_same_window_as_earliest():
    """`wait` is just 'pay in full on earliest', so whenever earliest exists the
    wait plan must be enumerated -- the two can never disagree."""
    profile = mk_profile(current_available_balance=1000.0, minimum_balance_to_keep=100.0,
                         methods={"full_payment"})
    request = mk_request(requested_amount=800.0,
                         desired_completion_date=START + timedelta(days=20))
    # a PROJECTED debit inside the trimmed tail (D35): the full window trips over
    # it, the tail mode ignores it for candidates after the request date
    flows = [CashFlow(START + timedelta(days=88), -500.0, "s:1", "recurring", "misc", "misc")]

    safe, earliest, plans = run(profile, request, [], flows, [])
    # D27: day 0 is held to the full 90 days, so the day-88 debit blocks today
    assert earliest == START + timedelta(days=1)
    assert not [p for p in plans if p.method == "full_payment"]   # full window fails
    waits = [p for p in plans if p.method == "wait"]
    assert waits and waits[0].payments == [(earliest, 800.0)]
    # judged over the full window but on the trimmed flow list (D35)
    assert check_horizon_for(request, earliest, POLICY) == 90
    assert waits[0].min_balance_after >= profile.minimum_balance_to_keep

    # push the first safe day out so `wait` is the plan that gets built
    late = mk_profile(current_available_balance=100.0, minimum_balance_to_keep=100.0,
                      methods={"full_payment"})
    flows = [
        CashFlow(START + timedelta(days=10), 2000.0, "event_1", "scheduled", "salary", "salary"),
        # a projection inside the trimmed tail, so the wait check ignores it (D35)
        CashFlow(START + timedelta(days=88), -5000.0, "s:1", "recurring", "misc", "misc"),
    ]
    safe, earliest, plans = run(late, request, [], flows, [])
    waits = [p for p in plans if p.method == "wait"]
    assert earliest == START + timedelta(days=10)
    assert waits and waits[0].payments == [(earliest, 800.0)]
    # min_balance_after is measured over the wait window, not the full 90 days
    assert waits[0].min_balance_after >= late.minimum_balance_to_keep


def test_wait_needs_full_payment_in_methods():
    streams = [mk_stream(stream_id="s:salary", category="salary", direction="credit", amount=200000.0,
                         description="Salary", anchor_event_id="event_1", flexibility="fixed",
                         minimum_allowed_amount=None, last_date=date(2026, 2, 20))]
    flows = flows_for(streams)
    profile = mk_profile(current_available_balance=25000.0, minimum_balance_to_keep=20000.0,
                         methods={"installments"})
    notes: list[str] = []
    _, _, plans = run(profile, mk_request(requested_amount=50000.0), [], flows, streams, notes)
    assert not [p for p in plans if p.method == "wait"]
    assert any("does not accept full_payment" in n for n in notes)
