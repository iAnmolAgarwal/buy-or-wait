"""Property tests for amount_safe_to_pay and earliest_full_payment_date.

CONTRACT engine/solver.py; PS:163 (earliest is preference-independent).
"""
from __future__ import annotations

from datetime import date, timedelta

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from buyorwait.types import CashFlow, Profile, Request
from buyorwait.engine.forecast import balance_path
from buyorwait.engine.policy import Policy
from buyorwait.engine.solver import (
    amount_safe_to_pay,
    check_horizon_for,
    earliest_full_payment_date,
    flows_for_candidate,
    tail_trim_from,
    trim_tail_recurring,
)

POLICY = Policy()                                      # earliest_window="tail" (D34)
DEADLINE_WINDOW = Policy(earliest_window="deadline")    # superseded D25 rule
FULL_WINDOW = Policy(earliest_window="full")
WIDE_TAIL = Policy(earliest_window="tail", tail_days=90)
START = date(2026, 3, 1)
ALL_METHODS = ["full_payment", "partial_payment", "installments"]

# Money is generated in whole cents so that the "one cent more breaks it"
# property is exact rather than subject to binary-float dust.
cents = st.integers(min_value=-500_000, max_value=500_000)


@st.composite
def scenario(draw):
    balance = draw(st.integers(min_value=0, max_value=2_000_000)) / 100.0
    minimum = draw(st.integers(min_value=0, max_value=1_000_000)) / 100.0
    requested = draw(st.integers(min_value=1, max_value=2_000_000)) / 100.0
    # a mix of confirmed and projected flows, so D35's trim has something to bite
    raw = draw(st.lists(
        st.tuples(st.integers(min_value=0, max_value=90), cents,
                  st.sampled_from(["scheduled", "recurring", "message"])),
        max_size=12,
    ))
    flows = [
        CashFlow(
            flow_date=START + timedelta(days=off),
            amount=c / 100.0,
            source_id=f"event_{i}",
            kind=kind,
            category="misc",
            label="misc",
            stream_id=(f"s:{i}" if kind == "recurring" else None),
        )
        for i, (off, c, kind) in enumerate(raw)
    ]
    profile = Profile(
        user_id="user_01",
        home_currency="INR",
        current_available_balance=balance,
        minimum_balance_to_keep=minimum,
        financial_priorities=[],
        protect_categories=[],
        reduce_categories=[],
        stop_categories=[],
        methods=set(draw(st.lists(st.sampled_from(ALL_METHODS), unique=True))),
        max_installment_months=draw(st.one_of(st.none(), st.integers(1, 12))),
    )
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
    return profile, flows, request


def _min_with(profile, flows, payments, horizon=None):
    horizon = POLICY.horizon_days if horizon is None else horizon
    return min(b for _, b in balance_path(profile, flows, START, horizon, payments))


# ---------------------------------------------------------------------------
# (1) amount_safe_to_pay is exactly the largest safe amount
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(scenario())
def test_amount_safe_to_pay_is_safe_and_maximal(data):
    profile, flows, request = data
    safe = amount_safe_to_pay(profile, flows, request, POLICY)

    assert 0.0 <= safe <= request.requested_amount            # PS:110
    assert round(safe, 2) == safe

    # Paying `safe` today never breaches the minimum -- unless the do-nothing
    # forecast already breaches it, in which case no payment at all is safe and
    # the contract answer is 0.
    base_safe = POLICY.meets_minimum(_min_with(profile, flows, []), profile.minimum_balance_to_keep)
    if base_safe:
        assert POLICY.meets_minimum(
            _min_with(profile, flows, [(START, safe)]), profile.minimum_balance_to_keep
        )
    else:
        assert safe == 0.0

    # and one cent more does, whenever the cap is the safety check (not the request)
    if safe < request.requested_amount:
        assert not POLICY.meets_minimum(
            _min_with(profile, flows, [(START, safe + 0.01)]), profile.minimum_balance_to_keep
        )


@settings(max_examples=200, deadline=None)
@given(scenario())
def test_amount_safe_to_pay_ignores_payment_method_preferences(data):
    profile, flows, request = data
    base = amount_safe_to_pay(profile, flows, request, POLICY)
    for methods in ([], ["full_payment"], ["installments"], ALL_METHODS):
        variant = Profile(**{**profile.__dict__, "methods": set(methods)})
        assert amount_safe_to_pay(variant, flows, request, POLICY) == base


# ---------------------------------------------------------------------------
# (2) earliest_full_payment_date is preference-independent (PS:163)
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(scenario())
def test_earliest_is_unchanged_when_methods_vary(data):
    profile, flows, request = data
    base = earliest_full_payment_date(profile, flows, request, POLICY)
    for methods in ([], ["full_payment"], ["partial_payment"], ["installments"], ALL_METHODS):
        variant = Profile(**{**profile.__dict__, "methods": set(methods)})
        assert earliest_full_payment_date(variant, flows, request, POLICY) == base
    # ... and independent of allows_partial_payment / max_installment_months too
    other = Profile(**{**profile.__dict__, "max_installment_months": 1})
    alt_request = Request(**{**request.__dict__, "allows_partial_payment": not request.allows_partial_payment})
    assert earliest_full_payment_date(other, flows, alt_request, POLICY) == base
    # D25 note: desired_completion_date IS an input (it sets the window), but it
    # is a property of the request, not of the user's method preferences, so
    # PS:163 preference-independence still holds.


def _fixture(balance, minimum, requested, deadline_offset, breaches):
    """One profile/flows/request triple with a debit on each day in `breaches`.

    Each entry is (day_offset, amount) or (day_offset, amount, kind); the default
    kind is "scheduled" (a confirmed fact). "recurring" marks a projection, which
    D35 lets the tail window ignore near the end of the horizon."""
    profile = Profile(
        user_id="user_01", home_currency="INR", current_available_balance=balance,
        minimum_balance_to_keep=minimum, financial_priorities=[], protect_categories=[],
        reduce_categories=[], stop_categories=[], methods={"full_payment"},
        max_installment_months=None,
    )
    flows = [
        CashFlow(START + timedelta(days=b[0]), -b[1], f"event_{i}",
                 b[2] if len(b) > 2 else "scheduled", "misc", "misc")
        for i, b in enumerate(breaches)
    ]
    request = Request(
        request_id="request_26", user_id="user_01", request_date=START, request_type="purchase",
        requested_amount=requested, desired_completion_date=START + timedelta(days=deadline_offset),
        allows_partial_payment=False, request_text="",
    )
    return profile, flows, request


def test_d25_a_breach_after_the_deadline_does_not_block_earliest():
    """The superseded D25 mode, kept as regression cover for earliest_window
    ="deadline": a day-70 debit is outside a day-5 candidate's 20-day window."""
    profile, flows, request = _fixture(
        balance=100.0, minimum=100.0, requested=800.0, deadline_offset=20,
        breaches=[(5, -900.0), (70, 500.0)],       # negative amount == a credit
    )
    assert check_horizon_for(request, START + timedelta(days=5), DEADLINE_WINDOW) == 20
    assert earliest_full_payment_date(
        profile, flows, request, DEADLINE_WINDOW) == START + timedelta(days=5)

    # full window: after paying 800 on day 5 the balance is 200, and the day-70
    # debit of 500 takes it below the minimum, so no candidate ever works
    assert check_horizon_for(request, START + timedelta(days=5), FULL_WINDOW) == 90
    assert earliest_full_payment_date(profile, flows, request, FULL_WINDOW) is None

    # D34/D35: the tail mode checks all 90 days (it only ignores projections in
    # the last 4), so it refuses the date the deadline window accepted.
    assert check_horizon_for(request, START + timedelta(days=5), POLICY) == 90
    assert earliest_full_payment_date(profile, flows, request, POLICY) is None


# ---------------------------------------------------------------------------
# D34: the tail window
# ---------------------------------------------------------------------------


def test_d34_tail_window_closes_the_deadline_windows_hole():
    """analysis/window_experiment2.py, request_29 shape: a near deadline let the
    deadline window bless a date whose balance then collapsed for weeks."""
    profile, flows, request = _fixture(
        balance=1000.0, minimum=100.0, requested=600.0, deadline_offset=5,
        breaches=[(20, 500.0)],
    )
    day1 = START + timedelta(days=1)

    # the hole: under D25 the day-1 candidate only had to survive to day 5
    assert check_horizon_for(request, day1, DEADLINE_WINDOW) == 5
    assert earliest_full_payment_date(profile, flows, request, DEADLINE_WINDOW) == day1

    # D34/D35: day 1 is checked over all 90 days, which contains the day-20 debit
    assert check_horizon_for(request, day1, POLICY) == 90
    assert earliest_full_payment_date(profile, flows, request, POLICY) != day1
    # paying 600 leaves 400, and the day-20 debit of 500 sinks it for good
    assert earliest_full_payment_date(profile, flows, request, POLICY) is None


def test_d35_a_confirmed_debit_in_the_tail_is_always_honoured():
    """D35 (1): a *scheduled* 500 debit on day 88 is a fact, not a projection, so
    it blocks every candidate -- the tail must not create a blind spot for it."""
    profile, flows, request = _fixture(
        balance=1000.0, minimum=100.0, requested=600.0, deadline_offset=30,
        breaches=[(88, 500.0, "scheduled")],
    )
    day1 = START + timedelta(days=1)
    # the window is the full 90 days under "tail" now; only the list is trimmed
    assert check_horizon_for(request, day1, POLICY) == 90
    assert trim_tail_recurring(flows, tail_trim_from(request, day1, POLICY)) == flows

    assert earliest_full_payment_date(profile, flows, request, POLICY) is None
    assert earliest_full_payment_date(profile, flows, request, FULL_WINDOW) is None


def test_d35_a_projected_occurrence_in_the_tail_is_ignored():
    """D35 (2): the same debit as a *projected* stream occurrence is the least
    reliable part of the forecast, so a later candidate may ignore it."""
    profile, flows, request = _fixture(
        balance=1000.0, minimum=100.0, requested=600.0, deadline_offset=30,
        breaches=[(88, 500.0, "recurring")],
    )
    day1 = START + timedelta(days=1)
    assert tail_trim_from(request, day1, POLICY) == START + timedelta(days=86)
    assert trim_tail_recurring(flows, tail_trim_from(request, day1, POLICY)) == []

    assert earliest_full_payment_date(profile, flows, request, POLICY) == day1
    # day 0 keeps the untrimmed forecast (D27), so it is not safe today
    assert tail_trim_from(request, START, POLICY) is None
    assert earliest_full_payment_date(profile, flows, request, FULL_WINDOW) is None


def test_d35_only_the_tail_of_the_projection_is_trimmed():
    """A projected debit before the blind spot still counts."""
    profile, flows, request = _fixture(
        balance=1000.0, minimum=100.0, requested=600.0, deadline_offset=30,
        breaches=[(85, 500.0, "recurring")],
    )
    assert trim_tail_recurring(flows, tail_trim_from(request, START + timedelta(days=1), POLICY)) == flows
    assert earliest_full_payment_date(profile, flows, request, POLICY) is None


def test_d35_tail_days_is_the_only_knob():
    _, flows, request = _fixture(
        1000.0, 0.0, 1.0, deadline_offset=10,
        breaches=[(88, 1.0, "recurring"), (88, 1.0, "scheduled")],
    )
    day1 = START + timedelta(days=1)
    assert tail_trim_from(request, day1, POLICY) == START + timedelta(days=86)
    assert tail_trim_from(request, day1, WIDE_TAIL) == START            # trims everything projected
    assert tail_trim_from(request, START, WIDE_TAIL) is None            # D27 still wins on day 0
    assert tail_trim_from(request, day1, FULL_WINDOW) is None
    assert tail_trim_from(request, day1, DEADLINE_WINDOW) is None
    assert tail_trim_from(request, day1, Policy(earliest_window="tail", tail_days=0)) == (
        START + timedelta(days=90)
    )
    # whatever the knob, confirmed flows survive the trim
    for policy in (POLICY, WIDE_TAIL, FULL_WINDOW):
        kept = trim_tail_recurring(flows, tail_trim_from(request, day1, policy))
        assert any(f.kind == "scheduled" for f in kept)


@settings(max_examples=200, deadline=None)
@given(scenario())
def test_d35_earliest_is_monotone_when_the_trimmed_projections_are_debits(data):
    """Trimming more projected tail weakens the check -- so the date can only move
    earlier: full (trims nothing) >= tail K=4 >= tail K=90.

    This only holds when the trimmed projections are debits. D35 removes whole
    projected flows, so dropping a projected *credit* makes the forecast worse,
    not better, and the ordering genuinely reverses (a recurring credit of 0.01
    on day 1 is earliest=day 1 under "full" and None under tail K=90)."""
    profile, flows, request = data
    assume(all(f.amount <= 0 for f in flows if f.kind == "recurring"))

    def key(policy):
        d = earliest_full_payment_date(profile, flows, request, policy)
        return date.max if d is None else d

    assert key(FULL_WINDOW) >= key(POLICY) >= key(WIDE_TAIL)


@settings(max_examples=200, deadline=None)
@given(scenario())
def test_d35_trimming_never_touches_confirmed_flows(data):
    """Whatever the mode, every non-recurring flow survives into the checked list
    -- the blind spot is for projections only."""
    _, flows, request = data
    confirmed = [f for f in flows if f.kind != "recurring"]
    for policy in (POLICY, WIDE_TAIL, FULL_WINDOW, DEADLINE_WINDOW):
        for i in (0, 1, 45, 90):
            d = START + timedelta(days=i)
            kept = flows_for_candidate(flows, request, d, policy)
            assert [f for f in kept if f.kind != "recurring"] == confirmed
            if policy is not POLICY or i == 0:
                continue
            # under "tail", projections survive iff they are before the blind spot
            cut = START + timedelta(days=POLICY.horizon_days - POLICY.tail_days)
            assert [f for f in kept if f.kind == "recurring"] == [
                f for f in flows if f.kind == "recurring" and f.flow_date < cut
            ]


def test_d27_the_request_date_candidate_always_uses_the_full_window():
    """Paying today is what amount_safe_to_pay measures, so day 0 is never judged
    on the short window -- earliest == request_date iff the full amount is safe."""
    profile, flows, request = _fixture(
        balance=1000.0, minimum=100.0, requested=800.0, deadline_offset=20,
        breaches=[(70, 500.0)],
    )
    assert check_horizon_for(request, START, POLICY) == POLICY.horizon_days
    assert check_horizon_for(request, START, FULL_WINDOW) == POLICY.horizon_days
    # paying 800 today leaves 200; the day-70 debit takes it to -300
    assert earliest_full_payment_date(profile, flows, request, POLICY) != START
    assert amount_safe_to_pay(profile, flows, request, POLICY) < request.requested_amount


def test_d25_window_stretches_to_the_candidate_when_it_is_past_the_deadline():
    """Superseded D25 mode: a candidate later than the deadline still has to
    survive up to itself."""
    profile, flows, request = _fixture(
        balance=1000.0, minimum=100.0, requested=800.0, deadline_offset=5,
        breaches=[(30, 500.0)],
    )
    assert check_horizon_for(request, START + timedelta(days=1), DEADLINE_WINDOW) == 5
    assert check_horizon_for(request, START + timedelta(days=40), DEADLINE_WINDOW) == 40
    # day 40 is unsafe: the day-30 debit is inside its own window
    assert not DEADLINE_WINDOW.meets_minimum(
        _min_with(profile, flows, [(START + timedelta(days=40), 800.0)], 40), 100.0
    )
    # day 0 is judged on the full window (D27) and fails on the day-30 debit;
    # day 1 onwards only has to survive to the day-5 deadline
    assert earliest_full_payment_date(
        profile, flows, request, DEADLINE_WINDOW) == START + timedelta(days=1)


def test_d25_window_never_exceeds_the_horizon():
    _, _, request = _fixture(1.0, 0.0, 1.0, deadline_offset=400, breaches=[])
    for policy in (DEADLINE_WINDOW, POLICY, FULL_WINDOW):
        assert check_horizon_for(request, START + timedelta(days=1), policy) <= policy.horizon_days
        assert check_horizon_for(request, START + timedelta(days=90), policy) == policy.horizon_days


def test_d25_deadline_before_the_request_date_still_covers_the_candidate():
    _, _, request = _fixture(1.0, 0.0, 1.0, deadline_offset=0, breaches=[])
    assert check_horizon_for(request, START, DEADLINE_WINDOW) == 90            # D27
    assert check_horizon_for(request, START + timedelta(days=12), DEADLINE_WINDOW) == 12


@settings(max_examples=200, deadline=None)
@given(scenario())
def test_earliest_is_the_first_safe_date_and_nothing_before_it_is_safe(data):
    """D25/D35: each candidate is judged over its own window and its own trimmed
    flow list, so the scan has to reproduce both."""
    profile, flows, request = data
    earliest = earliest_full_payment_date(profile, flows, request, POLICY)
    requested = float(request.requested_amount)

    def candidate_is_safe(d):
        # D35: each candidate is judged over its own window AND its own trimmed
        # flow list, so the scan has to reproduce both.
        window = check_horizon_for(request, d, POLICY)
        check_flows = flows_for_candidate(flows, request, d, POLICY)
        return POLICY.meets_minimum(
            _min_with(profile, check_flows, [(d, requested)], window),
            profile.minimum_balance_to_keep,
        )

    if earliest is None:
        assert not any(candidate_is_safe(START + timedelta(days=i)) for i in range(POLICY.horizon_days + 1))
        return

    assert START <= earliest <= START + timedelta(days=POLICY.horizon_days)
    assert candidate_is_safe(earliest)
    d = START
    while d < earliest:
        assert not candidate_is_safe(d)
        d += timedelta(days=1)


@settings(max_examples=300, deadline=None)
@given(scenario())
def test_earliest_equals_request_date_iff_full_amount_is_safe_today(data):
    """D27 restores the iff: the day-0 candidate is checked over the same full
    90-day window as amount_safe_to_pay, so the two columns can never disagree
    about today. This is what keeps the coherence gate satisfiable."""
    profile, flows, request = data
    assume(request.requested_amount > 0)
    earliest = earliest_full_payment_date(profile, flows, request, POLICY)
    safe = amount_safe_to_pay(profile, flows, request, POLICY)
    assert (earliest == START) == (safe == request.requested_amount)


@settings(max_examples=200, deadline=None)
@given(scenario())
def test_full_window_earliest_is_never_earlier_than_deadline_window_earliest(data):
    """The full window is a superset of the deadline window, so it can only ever
    push the date later (or to None).

    Stated against DEADLINE_WINDOW, not the default: "deadline" only shortens the
    window, which is monotone. D35's "tail" removes whole projected flows, credits
    included, so it is NOT comparable to "full" -- see
    test_d35_earliest_is_monotone_when_the_trimmed_projections_are_debits."""
    profile, flows, request = data
    deadline_earliest = earliest_full_payment_date(profile, flows, request, DEADLINE_WINDOW)
    full_earliest = earliest_full_payment_date(profile, flows, request, FULL_WINDOW)
    if full_earliest is not None:
        assert deadline_earliest is not None
        assert deadline_earliest <= full_earliest
