"""ingest/loader.py — fail-closed row validation on a synthetic fixture dataset."""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from buyorwait.ingest.loader import load_dataset  # noqa: E402

FIXTURE = os.path.join(ROOT, "fixtures", "ingest")


@pytest.fixture(scope="module")
def ds():
    return load_dataset(FIXTURE)


def test_load_never_raises_and_collects_errors(ds):
    assert isinstance(ds.load_errors, list)
    assert ds.load_errors, "fixture contains malformed rows on purpose"


def test_malformed_event_rows_are_skipped_not_raised(ds):
    for bad in ("ev_90", "ev_91", "ev_92"):
        assert bad not in ds.events_by_id
    joined = " | ".join(ds.load_errors)
    assert "bogus_status" in joined
    assert "not-a-date" in joined
    assert "sideways" in joined
    # good rows in the same file still loaded
    assert "ev_01" in ds.events_by_id


def test_malformed_rows_in_other_files_are_skipped(ds):
    assert "user_bad" not in ds.profiles          # unparseable balance
    assert "req_03" not in ds.requests            # allows_partial_payment='maybe'
    assert "req_01" in ds.requests
    msg_ids = {m.message_id for m in ds.messages_by_user["user_a"]}
    assert "msg_03" not in msg_ids                 # unparseable sent_at
    assert ("USD", "IDR") not in {(f, t) for _, f, t, _ in ds.rates.rows}


def test_blank_amount_stays_none(ds):
    ev = ds.events_by_id["ev_50"]
    assert ev.amount is None
    assert ev.amount_raw is None
    assert ev.amount_source == "missing"


def test_amount_converted_to_home_currency_on_event_date(ds):
    ev = ds.events_by_id["ev_40"]        # 100 USD on 2024-02-20, home INR, rate 84
    assert ev.amount_raw == 100.0
    assert ev.currency == "USD"
    assert ev.amount == pytest.approx(8400.0)
    assert ev.amount_source == "csv"

    early = ds.events_by_id["ev_41"]     # 2024-01-05 -> falls back to the 2024-01-15 row
    assert early.amount == pytest.approx(8300.0)

    other = ds.events_by_id["ev_95"]     # 50 EUR on 2024-02-20, home ZAR, rate 21
    assert other.amount == pytest.approx(1050.0)

    home = ds.events_by_id["ev_10"]      # already INR: never converted
    assert home.amount == home.amount_raw == 20000.0


def test_request_and_profile_parsing(ds):
    r = ds.requests["req_01"]
    assert r.allows_partial_payment is True       # 'TRUE' is case-insensitive
    assert r.request_date == date(2024, 3, 20)
    assert ds.requests["req_02"].allows_partial_payment is False

    p = ds.profiles["user_a"]
    assert p.methods == {"full_payment", "installments"}
    assert p.max_installment_months == 6
    assert ds.profiles["user_b"].max_installment_months is None
    assert p.reduce_categories == ["streaming"]


def test_sorting_rules(ds):
    evs = ds.events_by_user["user_a"]
    assert [e.event_date for e in evs] == sorted(e.event_date for e in evs)
    opts = ds.options_by_request["req_01"]
    assert [o.payment_option_id for o in opts] == ["option_01", "option_02"]
    msgs = ds.messages_by_user["user_a"]
    assert [m.message_id for m in msgs] == ["msg_01", "msg_02"]
    assert msgs[0].sent_at == date(2024, 3, 1)   # date part of the ISO timestamp


def test_image_path_shape(ds):
    img = ds.images_by_user["user_a"][0]
    assert img.path == os.path.join(FIXTURE, "media", "images", "img_01.png")
    assert os.path.isabs(img.path) and os.path.exists(img.path)
    assert img.related_event_id == "ev_50"


def test_missing_root_is_reported_not_raised(tmp_path):
    ds = load_dataset(str(tmp_path))
    assert ds.requests == {} and ds.profiles == {}
    assert any("file not found" in e for e in ds.load_errors)
