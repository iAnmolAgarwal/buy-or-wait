"""Smoke test against the real dataset/ (read-only)."""
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

from conftest import requires_dataset  # noqa: E402

DATASET = os.path.join(ROOT, "dataset")

pytestmark = requires_dataset


@dataclass
class StubPolicy:
    amount_estimator: str = "mean"
    min_occurrences: int = 3
    min_income_occurrences: int = 2


def _streams(ds, user_id, on, notes=None):
    eligible, _ = resolve_lifecycles(ds.events_by_user[user_id])
    return detect_streams(eligible, ds.profiles[user_id], on, StubPolicy(), notes)


@pytest.fixture(scope="module")
def ds():
    if not os.path.isdir(DATASET):
        pytest.skip("dataset/ not present")
    return load_dataset(DATASET)


def test_real_dataset_shape(ds):
    assert len(ds.requests) == 250
    assert len(ds.profiles) == 275
    assert len(ds.events_by_id) == 25342
    # 250 live requests + the 25 labelled sample requests
    assert len(ds.options_by_request) == 275
    assert len(ds.rates.rows) == 134
    assert ds.load_errors == []  # the shipped dataset has no malformed rows
    blank = [e for e in ds.events_by_id.values() if e.amount is None]
    assert len(blank) == 16
    assert all(e.amount_source == "missing" for e in blank)
    assert all(e.amount_raw is None for e in blank)


def test_every_request_has_a_profile_and_events(ds):
    for r in ds.requests.values():
        assert r.user_id in ds.profiles
        assert ds.events_by_user.get(r.user_id)


def test_no_conversion_errors_on_the_real_data(ds):
    fx_errors = [e for e in ds.load_errors if "rate" in e or "fx" in e]
    assert fx_errors == [], fx_errors


def test_foreign_amounts_are_converted(ds):
    foreign = [
        e
        for e in ds.events_by_id.values()
        if e.amount_raw is not None and e.currency != ds.profiles[e.user_id].home_currency
    ]
    assert foreign, "dataset has foreign-currency events"
    assert all(e.amount is not None and e.amount != e.amount_raw for e in foreign)


def test_image_paths_exist(ds):
    imgs = [i for v in ds.images_by_user.values() for i in v]
    assert len(imgs) == 16
    assert all(os.path.exists(i.path) for i in imgs)


def test_user_05_salary_ends_with_the_final_employer_payroll(ds):
    """PROBE rule 4 / D18: event_390 'Final employer payroll' (2025-10-15)."""
    notes: list[str] = []
    streams = _streams(ds, "user_05", date(2025, 11, 6), notes)
    assert salary_streams(streams) == []
    assert any("terminated by event_390" in n for n in notes)


def test_user_13_second_household_income_is_stale(ds):
    """PROBE rule 5 / D17: it stops at 2024-01-20, 47 days before the request."""
    notes: list[str] = []
    streams = _streams(ds, "user_13", date(2024, 3, 7), notes)
    income = salary_streams(streams)
    assert all("second household" not in s.description.lower() for s in income)
    assert any("event_1056" in s.event_ids for s in streams) is False
    assert any("stale" in n for n in notes)
    # the primary household salary survives, anchored on the scheduled March payroll
    assert len(income) == 1
    assert income[0].amount == pytest.approx(1343.54)
    assert income[0].last_date == date(2024, 3, 15)


def test_user_03_salary_lands_on_the_fifteenth(ds):
    """PROBE rule 3 / D16: the restated 'August 2019 net salary' (event_253, filed on
    the 31st) must not move payroll off the 15th."""
    income = salary_streams(_streams(ds, "user_03", date(2019, 9, 3)))
    assert len(income) == 1
    assert income[0].last_date.day == 15
    assert income[0].monthly is True
    assert "event_211" not in income[0].event_ids   # the one-off arrears credit


def test_user_03_streams(ds):
    request_date = date(2019, 9, 3)
    streams = _streams(ds, "user_03", request_date)
    by_cat = {}
    for s in streams:
        by_cat.setdefault(s.category, []).append(s)

    salary = by_cat["salary"]
    assert len(salary) == 1
    assert salary[0].direction == "credit"
    assert salary[0].monthly is True
    assert salary[0].last_date == date(2019, 8, 15)
    assert salary[0].amount == pytest.approx(4365000.0)
    assert next_occurrences(salary[0], request_date, date(2019, 12, 2))[:2] == [
        date(2019, 9, 15),
        date(2019, 10, 15),
    ]

    groceries = by_cat["groceries"]
    assert len(groceries) == 1
    assert groceries[0].cadence_days == 10
    assert groceries[0].monthly is False
    assert groceries[0].last_date == date(2019, 8, 29)
    assert next_occurrences(groceries[0], request_date, date(2019, 9, 20)) == [
        date(2019, 9, 8),
        date(2019, 9, 18),
    ]

    assert by_cat["rent"][0].monthly is True
