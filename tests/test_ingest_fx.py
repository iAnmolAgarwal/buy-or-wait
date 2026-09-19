"""ingest/fx.py — dated rate lookup, inverse pairs, USD routing."""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buyorwait.ingest import fx  # noqa: E402
from buyorwait.types import RateTable  # noqa: E402

RATES = RateTable(
    rows=[
        (date(2024, 1, 15), "USD", "INR", 83.0),
        (date(2024, 2, 15), "USD", "INR", 84.0),
        (date(2024, 1, 15), "USD", "EUR", 0.92),
        (date(2024, 2, 15), "USD", "EUR", 0.93),
        (date(2024, 1, 15), "EUR", "ZAR", 20.0),
        (date(2024, 2, 15), "EUR", "ZAR", 21.0),
    ]
)


def test_identity_needs_no_rate():
    assert fx.convert(123.45, "INR", "INR", date(2024, 3, 1), RATES) == 123.45
    rate, on = fx.rate_lookup("ZAR", "ZAR", date(2024, 3, 1), RATES)
    assert rate == 1.0 and on == date(2024, 3, 1)


def test_direct_pair_uses_nearest_date_on_or_before():
    assert fx.rate_lookup("USD", "INR", date(2024, 2, 20), RATES) == (84.0, date(2024, 2, 15))
    assert fx.rate_lookup("USD", "INR", date(2024, 2, 15), RATES) == (84.0, date(2024, 2, 15))
    assert fx.rate_lookup("USD", "INR", date(2024, 2, 14), RATES) == (83.0, date(2024, 1, 15))
    assert fx.convert(100, "USD", "INR", date(2024, 2, 20), RATES) == pytest.approx(8400.0)


def test_before_first_rate_falls_back_to_earliest_row():
    rate, used = fx.rate_lookup("USD", "INR", date(2023, 12, 1), RATES)
    assert (rate, used) == (83.0, date(2024, 1, 15))
    assert used > date(2023, 12, 1)  # caller can detect the stale conversion


def test_inverse_pair_is_one_over_rate():
    rate, used = fx.rate_lookup("INR", "USD", date(2024, 2, 20), RATES)
    assert rate == pytest.approx(1 / 84.0)
    assert used == date(2024, 2, 15)
    assert fx.convert(8400, "INR", "USD", date(2024, 2, 20), RATES) == pytest.approx(100.0)


def test_routes_via_usd_when_no_direct_or_inverse_pair():
    # EUR->INR: leg 1 is the inverse of USD->EUR, leg 2 is USD->INR.
    rate, used = fx.rate_lookup("EUR", "INR", date(2024, 2, 20), RATES)
    assert rate == pytest.approx((1 / 0.93) * 84.0)
    assert used == date(2024, 2, 15)
    assert fx.convert(10, "EUR", "INR", date(2024, 2, 20), RATES) == pytest.approx(
        10 * (1 / 0.93) * 84.0
    )


def test_multi_hop_route_when_usd_leg_is_missing():
    # ZAR has no USD pair: ZAR -> EUR -> USD -> INR.
    rate, _ = fx.rate_lookup("ZAR", "INR", date(2024, 2, 20), RATES)
    assert rate == pytest.approx((1 / 21.0) * (1 / 0.93) * 84.0)


def test_unroutable_currency_raises_value_error():
    with pytest.raises(ValueError):
        fx.rate_lookup("JPY", "INR", date(2024, 2, 20), RATES)
    with pytest.raises(ValueError):
        fx.convert(10, "JPY", "INR", date(2024, 2, 20), RATES)
