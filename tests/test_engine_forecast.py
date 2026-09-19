"""build_flows / balance_path / _occurrences (CONTRACT engine/forecast.py)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from buyorwait.types import Amendment, CashFlow, Event, Profile, RateTable, RecurringStream, Request
from buyorwait.engine.forecast import _convert, _occurrences, balance_path, build_flows
from buyorwait.engine.policy import Policy

RATES = RateTable(
    rows=[
        (date(2026, 1, 15), "USD", "INR", 83.0),
        (date(2026, 2, 15), "USD", "INR", 84.0),
        (date(2026, 1, 15), "EUR", "USD", 1.1),
    ]
)
POLICY = Policy()


def profile(**kw) -> Profile:
    base = dict(
        user_id="user_01",
        home_currency="INR",
        current_available_balance=100000.0,
        minimum_balance_to_keep=20000.0,
        financial_priorities=[],
        protect_categories=[],
        reduce_categories=["dining"],
        stop_categories=["subscription"],
        methods={"full_payment", "partial_payment", "installments"},
        max_installment_months=6,
    )
    base.update(kw)
    return Profile(**base)


def request(**kw) -> Request:
    base = dict(
        request_id="request_26",
        user_id="user_01",
        request_date=date(2026, 3, 1),
        request_type="purchase",
        requested_amount=50000.0,
        desired_completion_date=date(2026, 4, 30),
        allows_partial_payment=True,
        request_text="",
    )
    base.update(kw)
    return Request(**base)


def stream(**kw) -> RecurringStream:
    base = dict(
        stream_id="user_01:rent:0",
        category="rent",
        description="Monthly rent",
        direction="debit",
        cadence_days=30,
        monthly=True,
        amount=10000.0,
        anchor_event_id="event_10",
        last_date=date(2026, 2, 5),
        flexibility="fixed",
        minimum_allowed_amount=None,
        event_ids=["event_8", "event_9", "event_10"],
    )
    base.update(kw)
    return RecurringStream(**base)


def event(**kw) -> Event:
    base = dict(
        event_id="event_500",
        user_id="user_01",
        event_type="expense",
        description="School fee",
        category="education",
        direction="debit",
        amount=5000.0,
        amount_raw=5000.0,
        currency="INR",
        event_date=date(2026, 3, 20),
        settlement_date=None,
        status="scheduled",
        linked_event_id=None,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )
    base.update(kw)
    return Event(**base)


# ---------------------------------------------------------------------------
# _occurrences
# ---------------------------------------------------------------------------


def test_monthly_occurrences_keep_day_of_month_and_clamp():
    s = stream(last_date=date(2026, 1, 31), monthly=True)
    got = _occurrences(s, date(2026, 2, 1), date(2026, 5, 31))
    assert got == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)]


def test_cadence_occurrences_and_bounds():
    s = stream(monthly=False, cadence_days=7, last_date=date(2026, 3, 1))
    got = _occurrences(s, date(2026, 3, 1), date(2026, 3, 29))
    assert got == [date(2026, 3, 8), date(2026, 3, 15), date(2026, 3, 22), date(2026, 3, 29)]
    # never the anchor itself, never before `start`
    assert all(d > s.last_date for d in got)
    assert _occurrences(stream(monthly=False, cadence_days=0), date(2026, 3, 1), date(2026, 6, 1)) == []


# ---------------------------------------------------------------------------
# fx mirror
# ---------------------------------------------------------------------------


def test_convert_direct_inverse_and_via_usd():
    assert _convert(10, "INR", "INR", date(2026, 3, 1), RATES) == 10
    # nearest rate_date <= on
    assert _convert(1, "USD", "INR", date(2026, 3, 1), RATES) == pytest.approx(84.0)
    assert _convert(1, "USD", "INR", date(2026, 1, 20), RATES) == pytest.approx(83.0)
    # inverse
    assert _convert(84.0, "INR", "USD", date(2026, 3, 1), RATES) == pytest.approx(1.0)
    # via USD: EUR -> USD -> INR
    assert _convert(1, "EUR", "INR", date(2026, 3, 1), RATES) == pytest.approx(1.1 * 84.0)
    with pytest.raises(ValueError):
        _convert(1, "ZAR", "IDR", date(2026, 3, 1), RATES)


# ---------------------------------------------------------------------------
# build_flows
# ---------------------------------------------------------------------------


def test_streams_and_scheduled_events_become_signed_flows():
    p, r = profile(), request()
    rent = stream()
    salary = stream(
        stream_id="user_01:salary:0",
        category="salary",
        description="Monthly salary",
        direction="credit",
        amount=60000.0,
        anchor_event_id="event_20",
        last_date=date(2026, 2, 15),
        event_ids=["event_18", "event_19", "event_20"],
    )
    flows, notes = build_flows([rent, salary], [event()], [], p, r, RATES, POLICY)
    rents = [f for f in flows if f.stream_id == rent.stream_id]
    sals = [f for f in flows if f.stream_id == salary.stream_id]
    sched = [f for f in flows if f.kind == "scheduled"]
    assert all(f.amount == -10000.0 and f.kind == "recurring" for f in rents)
    assert all(f.amount == 60000.0 for f in sals)
    assert [f.flow_date for f in rents] == [date(2026, 3, 5), date(2026, 4, 5), date(2026, 5, 5)]
    assert len(sched) == 1 and sched[0].amount == -5000.0 and sched[0].source_id == "event_500"
    # flexibility / minimum carried over
    assert rents[0].flexibility == rent.flexibility


def test_scheduled_event_uses_settlement_date_and_is_clamped_to_request_date():
    p, r = profile(), request()
    ev = event(event_date=date(2026, 2, 20), settlement_date=date(2026, 3, 10), status="pending")
    flows, _ = build_flows([], [ev], [], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [date(2026, 3, 10)]

    past = event(event_date=date(2026, 2, 1), settlement_date=None, status="pending")
    flows, _ = build_flows([], [past], [], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [r.request_date]

    ineligible = event(status="settled", settlement_date=date(2026, 2, 1), event_date=date(2026, 2, 1))
    flows, _ = build_flows([], [ineligible], [], p, r, RATES, POLICY)
    assert flows == []


def test_amendment_cancel_removes_the_targeted_event():
    p, r = profile(), request()
    flows, notes = build_flows(
        [stream()], [event()], [Amendment(kind="cancel", source_id="message_1", target_event_id="event_500")],
        p, r, RATES, POLICY,
    )
    assert all(f.source_id != "event_500" for f in flows)
    assert any("cancel" in n for n in notes)


def test_amendment_delay_moves_the_event():
    p, r = profile(), request()
    am = Amendment(kind="delay", source_id="message_1", target_event_id="event_500", new_date=date(2026, 4, 2))
    flows, _ = build_flows([], [event()], [am], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [date(2026, 4, 2)]


def test_amendment_amend_amount_converts_currency_and_respects_effective_from():
    p, r = profile(), request()
    rent = stream()
    am = Amendment(
        kind="amend_amount",
        source_id="message_2",
        target_event_id="event_10",          # anchor of the rent stream
        new_amount=200.0,
        currency="USD",
        effective_from=date(2026, 4, 1),
    )
    flows, _ = build_flows([rent], [], [am], p, r, RATES, POLICY)
    by_date = {f.flow_date: f.amount for f in flows}
    assert by_date[date(2026, 3, 5)] == -10000.0                 # before effective_from: untouched
    assert by_date[date(2026, 4, 5)] == pytest.approx(-200 * 84.0)
    assert by_date[date(2026, 5, 5)] == pytest.approx(-200 * 84.0)


def test_amendment_amend_amount_pct_scales_matching_stream():
    p, r = profile(), request()
    am = Amendment(kind="amend_amount_pct", source_id="message_3", target_category="rent", pct_change=0.10)
    flows, _ = build_flows([stream()], [], [am], p, r, RATES, POLICY)
    assert all(f.amount == pytest.approx(-11000.0) for f in flows)


def test_amend_amount_pct_applies_to_every_occurrence_from_effective_from():  # D20
    p, r = profile(), request()
    rent = stream()   # occurrences 5 Mar, 5 Apr, 5 May
    no_gate = Amendment(kind="amend_amount_pct", source_id="m", target_category="rent", pct_change=0.12)
    flows, _ = build_flows([rent], [], [no_gate], p, r, RATES, POLICY)
    assert len(flows) == 3
    assert all(f.amount == pytest.approx(-11200.0) for f in flows)   # None -> all future

    gated = Amendment(kind="amend_amount_pct", source_id="m", target_category="rent",
                      pct_change=0.12, effective_from=date(2026, 4, 1))
    flows, _ = build_flows([rent], [], [gated], p, r, RATES, POLICY)
    by_date = {f.flow_date: f.amount for f in flows}
    assert by_date[date(2026, 3, 5)] == pytest.approx(-10000.0)
    assert by_date[date(2026, 4, 5)] == pytest.approx(-11200.0)
    assert by_date[date(2026, 5, 5)] == pytest.approx(-11200.0)      # not just the next one


def test_confirmed_event_supersedes_the_projection_it_duplicates():  # D19
    """'Next confirmed salary' on the 15th must not be counted twice alongside
    the projected salary occurrence three days away."""
    p, r = profile(), request()
    salary = stream(
        stream_id="user_01:salary:0", category="salary", description="Monthly salary",
        direction="credit", amount=60000.0, anchor_event_id="event_20",
        last_date=date(2026, 2, 13), event_ids=["event_20"],
    )
    confirmed = event(
        event_id="event_777", description="Next confirmed salary", category="salary",
        direction="credit", amount=61000.0, event_date=date(2026, 3, 15), status="scheduled",
    )
    flows, notes = build_flows([salary], [confirmed], [], p, r, RATES, POLICY)
    march = sorted(f for f in [x.flow_date for x in flows] if f.month == 3)
    assert march == [date(2026, 3, 15)]                     # 13 Mar projection dropped
    assert sum(f.amount for f in flows if f.flow_date.month == 3) == 61000.0
    assert any("dedupe" in n and "event_777" in n for n in notes)
    # later projected occurrences survive
    assert date(2026, 4, 13) in {f.flow_date for f in flows}


def test_dedupe_respects_the_window_the_category_and_the_direction():
    p, r = profile(), request()
    salary = stream(
        stream_id="user_01:salary:0", category="salary", description="Monthly salary",
        direction="credit", amount=60000.0, anchor_event_id="event_20",
        last_date=date(2026, 2, 10), event_ids=["event_20"],
    )
    far = event(event_id="event_1", category="salary", direction="credit", amount=1.0,
                event_date=date(2026, 3, 20), status="scheduled")   # 10 days away
    other_cat = event(event_id="event_2", category="bonus", direction="credit", amount=1.0,
                      event_date=date(2026, 3, 10), status="scheduled")
    other_dir = event(event_id="event_3", category="salary", direction="debit", amount=1.0,
                      event_date=date(2026, 3, 10), status="scheduled")
    for ev in (far, other_cat, other_dir):
        flows, notes = build_flows([salary], [ev], [], p, r, RATES, POLICY)
        assert date(2026, 3, 10) in {f.flow_date for f in flows if f.kind == "recurring"}
        assert not any("dedupe" in n for n in notes)


def test_income_change_and_income_end():
    p, r = profile(), request()
    salary = stream(
        stream_id="user_01:salary:0",
        category="salary",
        description="Monthly salary",
        direction="credit",
        amount=60000.0,
        anchor_event_id="event_20",
        last_date=date(2026, 2, 15),
        event_ids=["event_20"],
    )
    change = Amendment(
        kind="income_change", source_id="message_4", new_amount=70000.0,
        currency="INR", effective_from=date(2026, 4, 1),
    )
    flows, _ = build_flows([salary], [], [change], p, r, RATES, POLICY)
    by_date = {f.flow_date: f.amount for f in flows}
    assert by_date[date(2026, 3, 15)] == 60000.0
    assert by_date[date(2026, 4, 15)] == 70000.0

    end = Amendment(kind="income_end", source_id="message_5", effective_from=date(2026, 4, 1))
    flows, notes = build_flows([salary], [], [end], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [date(2026, 3, 15)]
    assert any("income_end" in n for n in notes)


def test_income_change_and_income_end_cover_every_occurrence(  # D20
):
    p, r = profile(), request()
    salary = stream(
        stream_id="user_01:salary:0", category="salary", description="Monthly salary",
        direction="credit", amount=60000.0, anchor_event_id="event_20",
        last_date=date(2026, 2, 15), event_ids=["event_20"],
    )
    base, _ = build_flows([salary], [], [], p, r, RATES, POLICY)
    assert len(base) == 3   # 15 Mar, 15 Apr, 15 May

    # effective_from None -> every occurrence after request_date, not just the next
    raise_all = Amendment(kind="income_change", source_id="m", new_amount=70000.0, currency="INR")
    flows, _ = build_flows([salary], [], [raise_all], p, r, RATES, POLICY)
    assert [f.amount for f in flows] == [70000.0, 70000.0, 70000.0]

    # gated: every occurrence on/after effective_from
    gated = Amendment(kind="income_change", source_id="m", new_amount=70000.0, currency="INR",
                      effective_from=date(2026, 3, 15))
    flows, _ = build_flows([salary], [], [gated], p, r, RATES, POLICY)
    assert [f.amount for f in flows] == [70000.0, 70000.0, 70000.0]   # inclusive of effective_from

    # income_end with no effective_from removes them all
    stop_all = Amendment(kind="income_end", source_id="m")
    flows, notes = build_flows([salary], [], [stop_all], p, r, RATES, POLICY)
    assert flows == []
    assert any("removed 3 income flow(s)" in n for n in notes)


def _salary_stream():
    return stream(
        stream_id="user_01:salary:0", category="salary", description="Monthly salary",
        direction="credit", amount=60000.0, anchor_event_id="event_20",
        last_date=date(2026, 2, 15), event_ids=["event_20"],
    )


def test_pending_ignore_never_removes_a_confirmed_recurring_stream():  # D24
    """Samples 04/11: a 'not yet credited' message about salary must not delete
    the confirmed payroll stream and zero out the user's income."""
    p, r = profile(), request()
    salary = _salary_stream()
    am = Amendment(kind="pending_ignore", source_id="message_03", target_category="salary")
    flows, notes = build_flows([salary], [], [am], p, r, RATES, POLICY)

    assert [f.flow_date for f in flows] == [date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15)]
    assert all(f.amount == 60000.0 and f.kind == "recurring" for f in flows)
    assert any("nothing to remove (confirmed stream untouched)" in n for n in notes)
    assert not any("removed" in n for n in notes)


def test_pending_ignore_removes_only_the_scheduled_credit_beside_the_stream():  # D24
    p, r = profile(), request()
    salary = _salary_stream()
    pending_bonus = event(
        event_id="event_900", description="Bonus (pending approval)", category="salary",
        direction="credit", amount=25000.0, event_date=date(2026, 4, 2), status="pending",
    )
    am = Amendment(kind="pending_ignore", source_id="message_03", target_category="salary")
    flows, notes = build_flows([salary], [pending_bonus], [am], p, r, RATES, POLICY)

    assert "event_900" not in {f.source_id for f in flows}
    assert len([f for f in flows if f.kind == "recurring"]) == 3
    assert sum(f.amount for f in flows) == 180000.0
    assert any("removed 1 unconfirmed credit(s)" in n for n in notes)


def test_pending_ignore_by_event_id_never_reaches_the_stream_it_anchors():  # D24
    """Even when the message names the stream's anchor event, only a scheduled
    flow with that id may be dropped."""
    p, r = profile(), request()
    salary = _salary_stream()
    am = Amendment(kind="pending_ignore", source_id="message_03", target_event_id="event_20")
    flows, notes = build_flows([salary], [], [am], p, r, RATES, POLICY)
    assert len(flows) == 3
    assert any("nothing to remove (confirmed stream untouched)" in n for n in notes)


def test_income_end_still_removes_the_recurring_stream():  # D24 guard check
    """income_end is the opposite case and must keep working: employment ended,
    so the payroll stream really does stop."""
    p, r = profile(), request()
    salary = _salary_stream()
    am = Amendment(kind="income_end", source_id="message_04", target_category="salary",
                   effective_from=date(2026, 4, 1))
    flows, notes = build_flows([salary], [], [am], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [date(2026, 3, 15)]
    assert any("removed 2 income flow(s)" in n for n in notes)


def test_delay_re_anchors_a_whole_stream():  # D21
    """Salary slips to the 23rd: the next payday moves and every later payday
    follows monthly from the new day-of-month (sample request_07)."""
    p = profile()
    r = request(request_date=date(2024, 9, 5), desired_completion_date=date(2024, 11, 14))
    salary = stream(
        stream_id="user_01:salary:0", category="salary", description="Monthly salary",
        direction="credit", amount=60000.0, anchor_event_id="event_20",
        last_date=date(2024, 8, 12), event_ids=["event_20"],
    )
    base, _ = build_flows([salary], [], [], p, r, RATES, POLICY)
    assert [f.flow_date for f in base] == [date(2024, 9, 12), date(2024, 10, 12), date(2024, 11, 12)]

    am = Amendment(kind="delay", source_id="message_11", target_category="salary",
                   new_date=date(2024, 9, 23))
    flows, notes = build_flows([salary], [], [am], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [date(2024, 9, 23), date(2024, 10, 23), date(2024, 11, 23)]
    assert all(f.amount == 60000.0 for f in flows)
    assert any("re-anchored" in n and "user_01:salary:0" in n for n in notes)


def test_delay_on_a_specific_event_id_moves_only_that_flow():  # D21
    p, r = profile(), request()
    rent = stream()
    am = Amendment(kind="delay", source_id="m", target_event_id="event_500", new_date=date(2026, 4, 2))
    flows, _ = build_flows([rent], [event()], [am], p, r, RATES, POLICY)
    moved = [f for f in flows if f.source_id == "event_500"]
    rents = [f.flow_date for f in flows if f.stream_id == rent.stream_id]
    assert [f.flow_date for f in moved] == [date(2026, 4, 2)]
    assert rents == [date(2026, 3, 5), date(2026, 4, 5), date(2026, 5, 5)]   # untouched


def test_re_anchored_occurrences_past_the_horizon_are_dropped():
    p, r = profile(), request()
    rent = stream()
    am = Amendment(kind="delay", source_id="m", target_category="rent", new_date=date(2026, 5, 20))
    flows, _ = build_flows([rent], [], [am], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [date(2026, 5, 20)]   # horizon ends 2026-05-30


def test_income_change_creates_a_salary_stream_when_there_is_none(  # D13
):
    """First-job user: no salary history, so income_change has nothing to amend
    and must create the income instead of silently dropping it."""
    p, r = profile(), request()
    am = Amendment(
        kind="income_change",
        source_id="message_9",
        new_amount=500.0,
        currency="USD",
        effective_from=date(2026, 3, 31),
    )
    flows, notes = build_flows([], [], [am], p, r, RATES, POLICY)
    # horizon end is 2026-05-30, so the May occurrence (31st) falls outside it
    assert [f.flow_date for f in flows] == [date(2026, 3, 31), date(2026, 4, 30)]
    assert all(
        f.amount == pytest.approx(500 * 84.0)
        and f.kind == "message"
        and f.source_id == "message_9"
        and f.category == "salary"
        for f in flows
    )
    assert "income_change created salary stream from message_9" in notes


def test_created_salary_stream_is_clamped_to_the_horizon():
    p, r = profile(), request()
    inside = Amendment(kind="income_change", source_id="m", new_amount=1000.0, currency="INR",
                       effective_from=date(2026, 1, 15))   # before request_date
    flows, _ = build_flows([], [], [inside], p, r, RATES, POLICY)
    assert [f.flow_date for f in flows] == [date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15)]

    outside = Amendment(kind="income_change", source_id="m", new_amount=1000.0, currency="INR",
                        effective_from=date(2027, 1, 15))
    flows, notes = build_flows([], [], [outside], p, r, RATES, POLICY)
    assert flows == []
    assert any("outside the horizon" in n for n in notes)


def test_income_change_still_amends_an_existing_stream_rather_than_creating_one():
    p, r = profile(), request()
    salary = stream(
        stream_id="user_01:salary:0", category="salary", description="Monthly salary",
        direction="credit", amount=60000.0, anchor_event_id="event_20",
        last_date=date(2026, 2, 15), event_ids=["event_20"],
    )
    am = Amendment(kind="income_change", source_id="m", new_amount=70000.0, currency="INR",
                   effective_from=date(2026, 3, 1))
    flows, notes = build_flows([salary], [], [am], p, r, RATES, POLICY)
    assert all(f.kind == "recurring" for f in flows)
    assert not any("created salary stream" in n for n in notes)


def test_new_income_creates_a_single_credit_without_a_matching_stream():
    p, r = profile(), request()
    am = Amendment(kind="new_income", source_id="message_10", new_amount=2500.0,
                   currency="INR", new_date=date(2026, 4, 4))
    flows, _ = build_flows([], [], [am], p, r, RATES, POLICY)
    assert [(f.flow_date, f.amount, f.kind) for f in flows] == [(date(2026, 4, 4), 2500.0, "message")]


def test_new_income_one_time_credit_and_pending_ignore():
    p, r = profile(), request()
    salary = stream(
        stream_id="user_01:salary:0", category="salary", description="Monthly salary",
        direction="credit", amount=60000.0, anchor_event_id="event_20",
        last_date=date(2026, 2, 15), event_ids=["event_20"],
    )
    ams = [
        Amendment(kind="new_income", source_id="message_6", new_amount=100.0, currency="USD",
                  new_date=date(2026, 3, 9)),
        Amendment(kind="one_time_credit", source_id="message_7", new_amount=5000.0, currency="INR"),
    ]
    flows, _ = build_flows([salary], [], ams, p, r, RATES, POLICY)
    extra = {(f.source_id, f.flow_date, round(f.amount, 2)) for f in flows if f.kind == "message"}
    assert ("message_6", date(2026, 3, 9), round(100 * 84.0, 2)) in extra
    # one_time_credit with no date lands on the next credit date in the forecast
    assert ("message_7", date(2026, 3, 9), 5000.0) in extra

    pending = event(direction="credit", status="pending", description="Refund", event_date=date(2026, 3, 20))
    ignore = Amendment(kind="pending_ignore", source_id="message_8", target_event_id="event_500")
    flows, notes = build_flows([], [pending], [ignore], p, r, RATES, POLICY)
    assert flows == []
    assert any("pending_ignore" in n for n in notes)


def test_amendments_apply_in_contract_order_cancel_before_amend():
    """cancel runs first, so an amend_amount on the same event finds nothing."""
    p, r = profile(), request()
    ams = [
        Amendment(kind="amend_amount", source_id="m2", target_event_id="event_500",
                  new_amount=99999.0, currency="INR"),
        Amendment(kind="cancel", source_id="m1", target_event_id="event_500"),
    ]
    flows, notes = build_flows([], [event()], ams, p, r, RATES, POLICY)
    assert flows == []
    assert notes[0].startswith("cancel")


# ---------------------------------------------------------------------------
# balance_path
# ---------------------------------------------------------------------------


def test_balance_path_is_inclusive_of_day_zero_and_day_horizon():
    p, r = profile(), request()
    f0 = CashFlow(r.request_date, -1000.0, "event_1", "scheduled", "x", "x")
    f90 = CashFlow(r.request_date + timedelta(days=90), -2000.0, "event_2", "scheduled", "x", "x")
    outside = CashFlow(r.request_date + timedelta(days=91), -5000.0, "event_3", "scheduled", "x", "x")
    path = balance_path(p, [f0, f90, outside], r.request_date, 90)
    assert len(path) == 91
    assert path[0] == (r.request_date, 99000.0)
    assert path[-1][1] == 97000.0


def test_balance_path_extra_payments_are_always_debits():
    p, r = profile(), request()
    path = balance_path(p, [], r.request_date, 90, [(r.request_date, 5000.0)])
    assert path[0][1] == 95000.0
    path = balance_path(p, [], r.request_date, 90, [(r.request_date, -5000.0)])
    assert path[0][1] == 95000.0
