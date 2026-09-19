"""ingest/recurrence.py — stream detection and projection."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from buyorwait.ingest.lifecycle import resolve_lifecycles  # noqa: E402
from buyorwait.ingest.loader import load_dataset  # noqa: E402
from buyorwait.ingest.recurrence import (  # noqa: E402
    detect_streams,
    next_occurrences,
    salary_streams,
)
from buyorwait.types import Event, RecurringStream  # noqa: E402

FIXTURE = os.path.join(ROOT, "fixtures", "ingest")
REQUEST_DATE = date(2024, 3, 20)


@dataclass
class StubPolicy:
    """Duck-typed stand-in for engine.policy.Policy (ingest never imports engine)."""

    amount_estimator: str = "mean"
    min_occurrences: int = 3
    min_income_occurrences: int = 2


@dataclass
class PolicyWithoutIncomeAttr:
    """Older policy object: `min_income_occurrences` must default to 2."""

    amount_estimator: str = "mean"
    min_occurrences: int = 3


def _credit(event_id, day, amount=1000.0, description="Payroll credit"):
    return Event(
        event_id=event_id,
        user_id="user_a",
        event_type="income",
        description=description,
        category="salary",
        direction="credit",
        amount=amount,
        amount_raw=amount,
        currency="INR",
        event_date=day,
        settlement_date=day,
        status="settled",
        linked_event_id=None,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )


@pytest.fixture(scope="module")
def streams():
    ds = load_dataset(FIXTURE)
    eligible, _ = resolve_lifecycles(ds.events_by_user["user_a"])
    return detect_streams(eligible, ds.profiles["user_a"], REQUEST_DATE, StubPolicy())


def one(streams, category, description=None):
    hits = [
        s
        for s in streams
        if s.category == category
        and (description is None or s.description.lower() == description.lower())
    ]
    assert len(hits) == 1, f"expected 1 {category}/{description} stream, got {hits}"
    return hits[0]


def test_weekly_grocery_stream(streams):
    s = one(streams, "groceries")
    assert s.cadence_days == 7 and s.monthly is False
    assert s.direction == "debit"
    assert s.amount == pytest.approx(14000 / 12, abs=0.01)
    assert s.anchor_event_id == "ev_18"
    assert s.last_date == date(2024, 3, 18)
    assert len(s.event_ids) == 12
    # descriptions vary inside the category -> still ONE stream
    assert s.description == "Supermarket basket"


def test_monthly_rent_stream(streams):
    s = one(streams, "rent")
    assert s.monthly is True and 28 <= s.cadence_days <= 31
    assert s.amount == pytest.approx(20000.0)
    assert s.anchor_event_id == "ev_12" and s.last_date == date(2024, 3, 3)


def test_monthly_salary_credit_stream(streams):
    s = one(streams, "salary")
    assert s.direction == "credit" and s.monthly is True
    assert s.amount == pytest.approx(60000.0)   # the one-off arrears credit is not in it
    assert s.last_date == date(2024, 3, 15)
    assert s.event_ids == ["ev_20", "ev_21", "ev_22"]


def test_category_split_into_two_subscription_streams(streams):
    video = one(streams, "streaming", "Video streaming plan")
    music = one(streams, "streaming", "Music streaming plan")
    assert video.amount == pytest.approx(500.0) and music.amount == pytest.approx(300.0)
    assert video.monthly and music.monthly
    assert video.flexibility == "reducible_or_stoppable" and video.minimum_allowed_amount == 250
    assert {video.stream_id, music.stream_id} == {"user_a:streaming:0", "user_a:streaming:1"}


def test_excluded_kinds_never_form_streams(streams):
    cats = {s.category for s in streams}
    assert "windfall" not in cats
    assert "investment" not in cats
    ids = {e for s in streams for e in s.event_ids}
    assert "ev_50" not in ids     # amount is None
    assert "ev_66" not in ids     # refund
    assert "ev_23" not in ids     # one-off arrears credit


def test_one_off_below_min_occurrences_is_not_projected(streams):
    assert all(s.category != "healthcare" for s in streams)
    assert all(s.category != "software" for s in streams)


def test_estimators():
    ds = load_dataset(FIXTURE)
    eligible, _ = resolve_lifecycles(ds.events_by_user["user_a"])
    profile = ds.profiles["user_a"]
    for estimator, expected in (
        ("median", 1150.0),
        ("last", 1350.0),
        ("max", 1400.0),
    ):
        s = one(
            detect_streams(eligible, profile, REQUEST_DATE, StubPolicy(estimator)),
            "groceries",
        )
        assert s.amount == pytest.approx(expected), estimator


def test_events_after_the_request_date_are_ignored():
    ds = load_dataset(FIXTURE)
    eligible, _ = resolve_lifecycles(ds.events_by_user["user_a"])
    s = one(
        detect_streams(
            eligible, ds.profiles["user_a"], date(2024, 2, 15), StubPolicy(min_occurrences=2)
        ),
        "salary",
    )
    assert s.event_ids == ["ev_20", "ev_21"]
    assert s.last_date == date(2024, 2, 15)


def test_two_payroll_credits_are_enough_for_an_income_stream():
    """D11: income needs only `min_income_occurrences` (2); debits still need 3."""
    events = [
        _credit("ev_i1", date(2024, 12, 15), 54120.0, "First-job payroll"),
        _credit("ev_i2", date(2025, 1, 15), 54120.0, "First-job payroll"),
    ]
    for policy in (StubPolicy(), PolicyWithoutIncomeAttr()):
        got = detect_streams(events, None, date(2025, 2, 5), policy)
        assert len(got) == 1, policy
        s = got[0]
        assert s.direction == "credit" and s.monthly is True
        assert s.amount == pytest.approx(54120.0)
        assert s.last_date == date(2025, 1, 15)
        assert next_occurrences(s, date(2025, 2, 5), date(2025, 3, 31)) == [
            date(2025, 2, 15),
            date(2025, 3, 15),
        ]
    # a two-occurrence DEBIT pattern is still not projected
    debits = [
        Event(**{**events[0].__dict__, "event_id": "ev_d1", "direction": "debit",
                 "event_type": "expense", "category": "shopping"}),
        Event(**{**events[1].__dict__, "event_id": "ev_d2", "direction": "debit",
                 "event_type": "expense", "category": "shopping"}),
    ]
    assert detect_streams(debits, None, date(2025, 2, 5), StubPolicy()) == []


def test_semi_monthly_income_projects_two_payments_a_month():
    """D12: paid on the 8th and the 20th -> gaps alternate 12/18, cadence is 15."""
    days = [
        date(2025, 11, 8), date(2025, 11, 20),
        date(2025, 12, 8), date(2025, 12, 20),
        date(2026, 1, 8), date(2026, 1, 20),
        date(2026, 2, 8), date(2026, 2, 20),
        date(2026, 3, 8), date(2026, 3, 20),
    ]
    events = [
        _credit(f"ev_s{i}", d, 500.0, f"Consulting invoice {i}")
        for i, d in enumerate(days, start=1)
    ]
    (s,) = detect_streams(events, None, date(2026, 4, 6), StubPolicy())
    assert s.cadence_days == 15
    assert s.monthly is False
    assert s.last_date == date(2026, 3, 20)
    horizon = next_occurrences(s, date(2026, 3, 20), date(2026, 6, 18))  # 90-day window
    assert len(horizon) == 6
    assert horizon[0] == date(2026, 4, 4) and horizon[-1] == date(2026, 6, 18)


def test_payroll_interrupted_by_leave_is_still_monthly():
    """Gaps of 31 and 90 days have a median of 60.5, but a series that always lands
    on the 15th is monthly with a missing month, not a two-monthly stream."""
    events = [
        _credit("ev_l1", date(2024, 12, 15), 62000.0, "Payroll before leave"),
        _credit("ev_l2", date(2025, 1, 15), 62000.0, "Payroll before leave"),
        _credit("ev_l3", date(2025, 4, 15), 62000.0, "Payroll after returning from leave"),
    ]
    (s,) = detect_streams(events, None, date(2025, 5, 7), StubPolicy())
    assert s.monthly is True and s.cadence_days == 30
    assert next_occurrences(s, date(2025, 5, 7), date(2025, 7, 31)) == [
        date(2025, 5, 15),
        date(2025, 6, 15),
        date(2025, 7, 15),
    ]


def test_income_is_grouped_by_normalised_description():
    """D16: 'Base salary' and 'Performance commission' share category 'salary' but
    are different money; month/year labels are not part of the identity."""
    events = [
        _credit("ev_b1", date(2025, 1, 15), 1000.0, "Base salary"),
        _credit("ev_b2", date(2025, 2, 15), 1000.0, "Base salary"),
        _credit("ev_b3", date(2025, 3, 15), 1200.0, "Base  SALARY "),   # case/space noise
        _credit("ev_c1", date(2025, 1, 25), 200.0, "Performance commission"),
        _credit("ev_c2", date(2025, 2, 25), 210.0, "Performance commission"),
        _credit("ev_c3", date(2025, 3, 25), 190.0, "Performance commission"),
    ]
    got = sorted(
        detect_streams(events, None, date(2025, 4, 2), StubPolicy()),
        key=lambda s: s.last_date.day,
    )
    assert len(got) == 2
    base, commission = got
    assert base.event_ids == ["ev_b1", "ev_b2", "ev_b3"]
    assert base.amount == pytest.approx(1200.0)      # newest, not the 1066.67 mean
    assert base.last_date == date(2025, 3, 15)
    assert commission.event_ids == ["ev_c1", "ev_c2", "ev_c3"]
    assert commission.amount == pytest.approx(190.0)
    assert commission.last_date == date(2025, 3, 25)


def test_month_labels_are_stripped_and_day_of_month_is_the_mode():
    """D16: 'August 2019 net salary' is the same stream as 'net salary', and a
    restatement filed on the 31st must not move payroll off the 15th."""
    events = [
        _credit("ev_n1", date(2019, 6, 15), 4365000.0, "June 2019 net salary"),
        _credit("ev_n2", date(2019, 7, 15), 4365000.0, "July 2019 net salary"),
        _credit("ev_n3", date(2019, 8, 15), 4365000.0, "August 2019 net salary"),
        _credit("ev_n4", date(2019, 8, 31), 4400000.0, "August 2019 net salary"),
    ]
    (s,) = detect_streams(events, None, date(2019, 9, 3), StubPolicy())
    assert len(s.event_ids) == 4                      # one stream, not four
    assert s.monthly is True
    assert s.last_date == date(2019, 8, 15)           # mode day-of-month, not the 31st
    assert s.amount == pytest.approx(4400000.0)       # newest occurrence's amount
    assert next_occurrences(s, date(2019, 9, 3), date(2019, 10, 31)) == [
        date(2019, 9, 15),
        date(2019, 10, 15),
    ]


def test_scheduled_salary_anchors_amount_and_day_without_being_an_occurrence():
    """D16: a scheduled 'Next confirmed salary' is the current contract figure, but
    the engine projects it as a scheduled event, so it must stay out of event_ids."""
    events = [
        _credit("ev_p1", date(2024, 2, 15), 12826.0, "Prorated first salary"),
        Event(
            event_id="ev_p2",
            user_id="user_a",
            event_type="income",
            description="Next confirmed salary",
            category="salary",
            direction="credit",
            amount=23320.0,
            amount_raw=23320.0,
            currency="INR",
            event_date=date(2024, 3, 15),
            settlement_date=date(2024, 3, 15),
            status="scheduled",
            linked_event_id=None,
            flexibility="fixed",
            minimum_allowed_amount=None,
        ),
    ]
    notes: list[str] = []
    (s,) = detect_streams(events, None, date(2024, 3, 3), StubPolicy(), notes)
    assert s.event_ids == ["ev_p1"]                   # the scheduled event is NOT an occurrence
    assert s.anchor_event_id == "ev_p2"
    assert s.amount == pytest.approx(23320.0)         # the confirmed figure, not 12826
    assert s.monthly is True and s.last_date == date(2024, 3, 15)
    assert next_occurrences(s, date(2024, 3, 3), date(2024, 5, 31)) == [
        date(2024, 4, 15),
        date(2024, 5, 15),
    ]                                                  # no double-paid 15 March
    assert any("ev_p2" in n and "not counted as an occurrence" in n for n in notes)


def test_final_employer_payroll_terminates_the_income_stream():
    """D18: employment has ended; nothing after it is projected."""
    events = [
        _credit("ev_f1", date(2025, 7, 15), 14740.0, "Payroll credit"),
        _credit("ev_f2", date(2025, 8, 15), 14740.0, "Payroll credit"),
        _credit("ev_f3", date(2025, 9, 15), 14740.0, "Payroll credit"),
        _credit("ev_f4", date(2025, 10, 15), 14740.0, "Final employer payroll"),
    ]
    notes: list[str] = []
    assert detect_streams(events, None, date(2025, 10, 20), StubPolicy(), notes) == []
    assert any("terminated by ev_f4" in n for n in notes)
    # without the final record the same history is a live monthly stream
    assert len(detect_streams(events[:3], None, date(2025, 10, 20), StubPolicy())) == 1


def test_stale_streams_are_dropped():
    """D17: nothing for 1.5 cadences (45 days for a monthly stream) -> stream is dead."""
    events = [
        _credit("ev_t1", date(2023, 10, 20), 990.0, "Second household income"),
        _credit("ev_t2", date(2023, 11, 20), 770.0, "Second household income"),
        _credit("ev_t3", date(2023, 12, 20), 950.0, "Second household income"),
        _credit("ev_t4", date(2024, 1, 20), 880.0, "Second household income"),
    ]
    notes: list[str] = []
    assert detect_streams(events, None, date(2024, 3, 7), StubPolicy(), notes) == []  # 47 days
    assert any("stale" in n for n in notes)
    assert len(detect_streams(events, None, date(2024, 2, 29), StubPolicy())) == 1    # 40 days

    weekly = [
        Event(**{**_credit(f"ev_w{i}", d, 500.0, "Bulk pantry shop").__dict__,
                 "direction": "debit", "event_type": "expense", "category": "groceries"})
        for i, d in enumerate(
            [date(2024, 1, 1), date(2024, 1, 8), date(2024, 1, 15), date(2024, 1, 22)], start=1
        )
    ]
    assert detect_streams(weekly, None, date(2024, 2, 8), StubPolicy()) == []   # 17 > 1.5*7
    assert len(detect_streams(weekly, None, date(2024, 1, 30), StubPolicy())) == 1  # 8 <= 10.5


def test_salary_streams_helper(streams):
    got = salary_streams(streams)
    assert [s.category for s in got] == ["salary"]
    assert got[0].direction == "credit"
    assert all(s.category != "windfall" for s in got)
    assert salary_streams([]) == []


def _stream(last_date, cadence, monthly):
    return RecurringStream(
        stream_id="u:x:0",
        category="x",
        description="x",
        direction="debit",
        cadence_days=cadence,
        monthly=monthly,
        amount=100.0,
        anchor_event_id="e1",
        last_date=last_date,
        flexibility="fixed",
        minimum_allowed_amount=None,
        event_ids=["e1"],
    )


def test_next_occurrences_fixed_cadence_respects_bounds():
    s = _stream(date(2024, 3, 1), 7, False)
    got = next_occurrences(s, date(2024, 3, 1), date(2024, 3, 25))
    assert got == [date(2024, 3, 8), date(2024, 3, 15), date(2024, 3, 22)]
    # strictly after last_date, and never past `end`
    assert next_occurrences(s, date(2024, 3, 1), date(2024, 3, 1)) == []
    # `start` clips the head of the series
    assert next_occurrences(s, date(2024, 3, 16), date(2024, 3, 25)) == [date(2024, 3, 22)]
    assert next_occurrences(s, date(2024, 4, 1), date(2024, 3, 1)) == []


def test_next_occurrences_monthly_clamps_to_month_length():
    s = _stream(date(2024, 1, 31), 30, True)
    got = next_occurrences(s, date(2024, 1, 31), date(2024, 5, 15))
    assert got == [
        date(2024, 2, 29),   # leap year clamp
        date(2024, 3, 31),
        date(2024, 4, 30),   # 30-day month clamp
    ]
    s2 = _stream(date(2023, 1, 31), 30, True)
    assert next_occurrences(s2, date(2023, 2, 1), date(2023, 3, 5))[0] == date(2023, 2, 28)


def test_next_occurrences_zero_cadence_is_safe():
    assert next_occurrences(_stream(date(2024, 3, 1), 0, False), date(2024, 3, 1), date(2025, 1, 1)) == []
