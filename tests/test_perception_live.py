"""ONE live smoke test. Skipped unless LIVE=1.

    LIVE=1 python -m pytest tests/test_perception_live.py -q -m live -s

Extracts image_01 (user_03 / event_253) and interprets message_02 for request_03.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from buyorwait.perception.client import ModelClient
from buyorwait.perception.run import PerceptionContext, run_perception
from buyorwait.types import Event, ImageRef, Message, Profile, RateTable, Request

from conftest import requires_dataset, requires_media  # noqa: E402

pytestmark = [pytest.mark.live, requires_dataset, requires_media]


def _rows(name):
    with open(os.path.join(ROOT, "dataset", name), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _date(value):
    return date.fromisoformat(value[:10]) if value else None


def _float(value):
    return float(value) if value not in ("", None) else None


@pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 to run")
def test_live_smoke_image_01_and_message_02(capsys):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY missing from .env"

    p = next(r for r in _rows("financial_profiles.csv") if r["user_id"] == "user_03")
    profile = Profile(
        user_id=p["user_id"], home_currency=p["home_currency"],
        current_available_balance=float(p["current_available_balance"]),
        minimum_balance_to_keep=float(p["minimum_balance_to_keep"]),
        financial_priorities=p["financial_priorities"].split("|"),
        protect_categories=p["expense_categories_to_protect"].split("|"),
        reduce_categories=p["expense_categories_user_is_willing_to_reduce"].split("|"),
        stop_categories=p["expense_categories_user_is_willing_to_stop"].split("|"),
        methods=set(p["payment_methods_user_will_consider"].split("|")),
        max_installment_months=int(p["max_installment_months"] or 0) or None)

    # request_03 is one of the labelled samples, not in requests.csv (request_26+).
    all_requests = _rows("requests.csv") + _rows("sample_requests.csv")
    r = next(x for x in all_requests if x["request_id"] == "request_03")
    request = Request(
        request_id=r["request_id"], user_id=r["user_id"],
        request_date=_date(r["request_date"]), request_type=r["request_type"],
        requested_amount=float(r["requested_amount"]),
        desired_completion_date=_date(r["desired_completion_date"]),
        allows_partial_payment=r["allows_partial_payment"].lower() == "true",
        request_text=r["request_text"])

    events = {}
    for e in _rows("financial_events.csv"):
        if e["user_id"] != "user_03":
            continue
        events[e["event_id"]] = Event(
            event_id=e["event_id"], user_id=e["user_id"], event_type=e["event_type"],
            description=e["description"], category=e["category"],
            direction=e["direction"], amount=_float(e["amount"]),
            amount_raw=_float(e["amount"]), currency=e["currency"],
            event_date=_date(e["event_date"]),
            settlement_date=_date(e["settlement_date"]), status=e["status"],
            linked_event_id=e["linked_event_id"] or None,
            flexibility=e["flexibility"],
            minimum_allowed_amount=_float(e["minimum_allowed_amount"]),
            amount_source="csv" if e["amount"] else "missing")

    m = next(x for x in _rows("messages.csv") if x["message_id"] == "message_02")
    message = Message(message_id=m["message_id"], user_id=m["user_id"],
                      request_id=m["request_id"] or None,
                      related_event_id=m["related_event_id"] or None,
                      sent_at=_date(m["sent_at"]), source_type=m["source_type"],
                      message_text=m["message_text"])

    i = next(x for x in _rows("images.csv") if x["image_id"] == "image_01")
    image = ImageRef(image_id=i["image_id"], user_id=i["user_id"],
                     request_id=i["request_id"] or None,
                     related_event_id=i["related_event_id"] or None,
                     path=os.path.join(ROOT, "dataset", "media", "images",
                                       f"{i['image_id']}.png"))

    rates = RateTable(rows=[(_date(x["rate_date"]), x["from_currency"],
                             x["to_currency"], float(x["rate"]))
                            for x in _rows("exchange_rates.csv")])

    client = ModelClient(model="claude-sonnet-5",
                         cache_dir=os.path.join(ROOT, "cache"))
    ctx = PerceptionContext(request=request, profile=profile, messages=[message],
                            images=[image], events_by_id=events, rates=rates,
                            client=client)
    result = run_perception(ctx)

    with capsys.disabled():
        print("\n===== LIVE PERCEPTION SMOKE =====")
        print(f"request_id      : {result.request_id}")
        print(f"model_calls     : {result.model_calls}  tool_steps: {result.tool_steps}")
        print(f"tokens in/out   : {result.input_tokens}/{result.output_tokens}"
              f"  cached: {result.cached}")
        print(f"fallbacks       : {result.fallbacks}")
        for image_id, ex in result.image_extractions.items():
            print(f"\n-- image {image_id}")
            print(f"   amount      : {ex.amount} {ex.currency}")
            print(f"   doc_date    : {ex.doc_date}   due_date: {ex.due_date}")
            print(f"   description : {ex.description}")
            print(f"   confidence  : {ex.confidence}")
        print(f"\n-- amendments ({len(result.amendments)})")
        for a in result.amendments:
            print(f"   kind={a.kind} source={a.source_id} "
                  f"target_event={a.target_event_id} target_category={a.target_category} "
                  f"new_amount={a.new_amount} {a.currency} new_date={a.new_date} "
                  f"effective_from={a.effective_from} pct={a.pct_change} "
                  f"conf={a.confidence}\n     note: {a.note}")
        print(f"\nusage_snapshot  : {client.usage_snapshot()}")
        print("=================================\n")

    assert result.fallbacks == [], result.fallbacks
    assert "image_01" in result.image_extractions
    assert result.image_extractions["image_01"].amount is not None
