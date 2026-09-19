"""Offline tests for perception/. No network: every ModelClient gets a fake
`transport` callable that returns plain dicts shaped like an SDK response.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buyorwait.perception import prompts
from buyorwait.perception.cache import ResponseCache, cache_key
from buyorwait.perception.client import ModelClient, validate_against_schema
from buyorwait.perception.extract import extract_image
from buyorwait.perception.fallback import Fallback
from buyorwait.perception.run import PerceptionContext, run_perception, sanitise_amendments
from buyorwait.perception.tools import ToolBox, lookup_rate
from buyorwait.types import (Amendment, Event, ImageRef, Message, Profile, RateTable,
                             Request)

SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": ["number", "null"]},
                   "kind": {"type": "string", "enum": ["a", "b"]}},
    "required": ["amount", "kind"],
}


def response(blocks, in_tok=10, out_tok=5):
    return {"content": blocks, "stop_reason": "tool_use",
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}


def emit(payload, tool_id="t1"):
    return response([{"type": "tool_use", "id": tool_id, "name": "emit_result",
                      "input": payload}])


def make_client(transport, tmp_path, **kw):
    return ModelClient(model="fake-model", cache_dir=str(tmp_path / "cache"),
                       transport=transport, sleep=lambda _s: None, **kw)


# ---------------------------------------------------------------------------
# schema validator
# ---------------------------------------------------------------------------

def test_validator_accepts_and_rejects():
    assert validate_against_schema({"amount": 1.5, "kind": "a"}, SCHEMA) == []
    assert validate_against_schema({"amount": None, "kind": "b"}, SCHEMA) == []
    assert validate_against_schema({"kind": "a"}, SCHEMA)          # missing required
    assert validate_against_schema({"amount": "x", "kind": "a"}, SCHEMA)  # wrong type
    assert validate_against_schema({"amount": 1, "kind": "z"}, SCHEMA)    # bad enum
    assert validate_against_schema({"amount": True, "kind": "a"}, SCHEMA)  # bool != number


# ---------------------------------------------------------------------------
# retry path: schema mismatch then success
# ---------------------------------------------------------------------------

def test_retry_after_schema_mismatch(tmp_path):
    seen = []

    def transport(**kw):
        seen.append(kw)
        if len(seen) == 1:
            return emit({"amount": "not-a-number", "kind": "a"})
        return emit({"amount": 42.0, "kind": "a"})

    client = make_client(transport, tmp_path)
    result, stats = client.structured_call(request_id="request_01", system="s",
                                           messages=[{"role": "user", "content": "hi"}],
                                           schema=SCHEMA)
    assert result == {"amount": 42.0, "kind": "a"}
    assert len(seen) == 2
    assert stats.calls == 1            # the successful attempt
    assert stats.cached is False
    # Both calls are recorded in the usage accumulator.
    assert client.usage_snapshot()["fake-model"]["calls"] == 2
    # No other tools -> emit_result is forced.
    assert seen[0]["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert [t["name"] for t in seen[0]["tools"]] == ["emit_result"]
    assert seen[0]["tools"][0]["input_schema"] is SCHEMA


def test_three_failures_raise_fallback_once(tmp_path, caplog):
    calls = {"n": 0}

    def transport(**kw):
        calls["n"] += 1
        return emit({"amount": 1, "kind": "nope"})     # always fails the enum

    client = make_client(transport, tmp_path)
    with caplog.at_level("WARNING", logger="perception"):
        with pytest.raises(Fallback) as excinfo:
            client.structured_call(request_id="request_02", system="s",
                                   messages=[{"role": "user", "content": "hi"}],
                                   schema=SCHEMA)
    assert calls["n"] == 3
    assert excinfo.value.request_id == "request_02"
    give_up_lines = [r for r in caplog.records if "perception fallback" in r.message]
    assert len(give_up_lines) == 1     # give_up logged exactly once


def test_non_retryable_error_propagates(tmp_path):
    def transport(**kw):
        raise ValueError("boom")

    client = make_client(transport, tmp_path)
    with pytest.raises(ValueError):
        client.structured_call(request_id="request_03", system="s",
                               messages=[{"role": "user", "content": "hi"}],
                               schema=SCHEMA)


# ---------------------------------------------------------------------------
# bounded tool loop
# ---------------------------------------------------------------------------

def test_tool_loop_is_bounded_then_forces_emit(tmp_path):
    choices = []

    def transport(**kw):
        choices.append(kw["tool_choice"])
        if kw["tool_choice"]["type"] == "auto":
            return response([{"type": "tool_use", "id": f"t{len(choices)}",
                              "name": "get_rate",
                              "input": {"date": "2024-01-01", "from_ccy": "USD",
                                        "to_ccy": "INR"}}])
        return emit({"amount": 1.0, "kind": "b"})

    handled = []

    def handler(name, payload):
        handled.append(name)
        return '{"rate": 83.0}'

    client = make_client(transport, tmp_path)
    result, stats = client.structured_call(
        request_id="request_04", system="s",
        messages=[{"role": "user", "content": "hi"}], schema=SCHEMA,
        tools=[{"name": "get_rate", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}],
        tool_handler=handler, max_tool_steps=3)
    assert result == {"amount": 1.0, "kind": "b"}
    assert handled == ["get_rate"] * 3
    assert stats.tool_steps == 3
    assert [c["type"] for c in choices] == ["auto", "auto", "auto", "tool"]


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def test_cache_keys_are_request_isolated(tmp_path):
    payload = {"prompt": "identical", "n": 1}
    k1 = cache_key("request_01", payload)
    k2 = cache_key("request_02", payload)
    assert k1 != k2

    cache = ResponseCache(str(tmp_path))
    cache.put(k1, {"amount": 1.0, "kind": "a"})
    assert cache.get(k1) == {"amount": 1.0, "kind": "a"}
    assert cache.get(k2) is None            # no cross-hit


def test_cached_response_costs_zero_calls(tmp_path):
    hits = {"n": 0}

    def transport(**kw):
        hits["n"] += 1
        return emit({"amount": 7.0, "kind": "a"})

    client = make_client(transport, tmp_path)
    kwargs = dict(request_id="request_05", system="s",
                  messages=[{"role": "user", "content": "hi"}], schema=SCHEMA)
    first, s1 = client.structured_call(**kwargs)
    second, s2 = client.structured_call(**kwargs)
    assert first == second
    assert hits["n"] == 1
    assert (s1.calls, s1.cached) == (1, False)
    assert (s2.calls, s2.cached) == (0, True)
    assert client.usage_snapshot()["fake-model"]["cache_hits"] == 1

    # A different request_id with the same payload must miss.
    third, s3 = client.structured_call(**{**kwargs, "request_id": "request_06"})
    assert hits["n"] == 2
    assert s3.cached is False


# ---------------------------------------------------------------------------
# fixtures for run_perception
# ---------------------------------------------------------------------------

def make_profile():
    return Profile(user_id="user_03", home_currency="IDR",
                   current_available_balance=5810300.0,
                   minimum_balance_to_keep=2668700.0,
                   financial_priorities=[], protect_categories=[],
                   reduce_categories=[], stop_categories=[],
                   methods={"full_payment"}, max_installment_months=2)


def make_request(request_id="request_03"):
    return Request(request_id=request_id, user_id="user_03",
                   request_date=date(2019, 9, 3), request_type="education",
                   requested_amount=5491000.0,
                   desired_completion_date=date(2019, 11, 15),
                   allows_partial_payment=False, request_text="course")


def make_event():
    return Event(event_id="event_253", user_id="user_03", event_type="income",
                 description="August 2019 net salary", category="salary",
                 direction="credit", amount=None, amount_raw=None, currency="IDR",
                 event_date=date(2019, 8, 31), settlement_date=date(2019, 8, 31),
                 status="settled", linked_event_id=None, flexibility="fixed",
                 minimum_allowed_amount=None, amount_source="missing")


def make_message():
    return Message(message_id="message_02", user_id="user_03", request_id="request_03",
                   related_event_id=None, sent_at=date(2019, 8, 31),
                   source_type="employer", message_text="Gaji rutin dikonfirmasi.")


# ---------------------------------------------------------------------------
# run_perception
# ---------------------------------------------------------------------------

def test_no_messages_no_images_makes_no_api_call(tmp_path):
    def transport(**kw):
        raise AssertionError("the API must not be called")

    ctx = PerceptionContext(request=make_request(), profile=make_profile(),
                            messages=[], images=[], events_by_id={},
                            rates=RateTable(rows=[]),
                            client=make_client(transport, tmp_path))
    result = run_perception(ctx)
    assert result.model_calls == 0
    assert result.cached is False
    assert result.amendments == [] and result.image_extractions == {}
    assert result.fallbacks == []


def test_run_perception_collects_amendments(tmp_path):
    def transport(**kw):
        return emit({"amendments": [{
            "kind": "income_change", "source_id": "message_02",
            "target_event_id": None, "target_category": "salary",
            "new_amount": 4365000.0, "currency": "IDR", "new_date": None,
            "effective_from": "2019-09-15", "pct_change": None,
            "confidence": 0.9, "note": "regular salary confirmed"}]})

    ctx = PerceptionContext(request=make_request(), profile=make_profile(),
                            messages=[make_message()], images=[],
                            events_by_id={"event_253": make_event()},
                            rates=RateTable(rows=[]),
                            client=make_client(transport, tmp_path))
    result = run_perception(ctx)
    assert result.model_calls == 1
    assert len(result.amendments) == 1
    amendment = result.amendments[0]
    assert amendment.kind == "income_change"
    assert amendment.effective_from == date(2019, 9, 15)   # parsed to a real date
    assert result.fallbacks == []


def test_run_perception_drops_bad_dates_and_notes_it(tmp_path, caplog):
    def transport(**kw):
        return emit({"amendments": [
            {"kind": "income_change", "source_id": "message_02",
             "target_event_id": None, "target_category": "salary",
             "new_amount": 100.0, "currency": "IDR", "new_date": None,
             "effective_from": "2019-13-45", "pct_change": None,
             "confidence": 0.9, "note": "bad date"},
            {"kind": "delay", "source_id": "message_02", "target_event_id": None,
             "target_category": "salary", "new_amount": None, "currency": None,
             "new_date": None, "effective_from": None, "pct_change": None,
             "confidence": 0.8, "note": "delay with no date"},
            {"kind": "none", "source_id": "message_02", "target_event_id": None,
             "target_category": None, "new_amount": None, "currency": None,
             "new_date": None, "effective_from": None, "pct_change": None,
             "confidence": 1.0, "note": "keeper"}]})

    ctx = PerceptionContext(request=make_request(), profile=make_profile(),
                            messages=[make_message()], images=[],
                            events_by_id={}, rates=RateTable(rows=[]),
                            client=make_client(transport, tmp_path))
    with caplog.at_level("WARNING", logger="perception"):
        result = run_perception(ctx)
    assert [a.kind for a in result.amendments] == ["none"]
    assert len(result.fallbacks) == 2
    assert all("dropped amendment" in f for f in result.fallbacks)
    assert any("dropped amendment" in r.message for r in caplog.records)


def test_run_perception_catches_fallback(tmp_path):
    def transport(**kw):
        return emit({"wrong": "shape"})

    ctx = PerceptionContext(request=make_request(), profile=make_profile(),
                            messages=[make_message()], images=[], events_by_id={},
                            rates=RateTable(rows=[]),
                            client=make_client(transport, tmp_path))
    result = run_perception(ctx)
    assert result.amendments == []
    assert result.image_extractions == {}
    assert result.model_calls == 0
    assert len(result.fallbacks) == 1 and "failed after 3 attempts" in result.fallbacks[0]


def test_sanitise_amendments_passes_dates_through():
    kept, notes = sanitise_amendments(
        [Amendment(kind="delay", source_id="message_02", new_date="2025-02-23")],
        "request_07")
    assert notes == []
    assert kept[0].new_date == date(2025, 2, 23)


# ---------------------------------------------------------------------------
# image prompt (orchestrator decision D26)
# ---------------------------------------------------------------------------

def test_image_user_text_carries_settlement_date_and_status():
    text = prompts.image_user_text(
        image_id="image_05", event_description="telecom bill", event_category="utilities",
        event_currency="INR", event_date="2026-01-28", home_currency="INR",
        event_settlement_date="2026-02-10", event_status="pending")
    assert "event settlement date: 2026-02-10" in text
    assert "event status: pending" in text
    assert "applies on the event settlement date" in text
    assert "largest visible subtotal" in text


def test_image_user_text_marks_missing_settlement_date():
    text = prompts.image_user_text(
        image_id="image_04", event_description="grocery delivery", event_category="groceries",
        event_currency="INR", event_date="2026-01-28", home_currency="INR")
    assert "event settlement date: (unknown)" in text
    assert "event status: (unknown)" in text


def test_image_system_states_the_two_d26_rules():
    system = prompts.IMAGE_SYSTEM
    assert "CUT-OFF / CROPPED DOCUMENTS" in system
    assert "Item Bill" in system
    assert "Return null ONLY when no monetary amount" in system
    assert "AMOUNT THAT DEPENDS ON THE PAYMENT DATE" in system
    assert "return the LARGER amount" in system


def test_extract_image_passes_event_settlement_date_into_the_prompt(tmp_path):
    png = tmp_path / "image_05.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    sent = {}

    def transport(**kw):
        sent["text"] = kw["messages"][0]["content"][1]["text"]
        sent["system"] = kw["system"]
        return emit({"amount": 822.05, "currency": "INR", "doc_date": "2026-01-28",
                     "due_date": "2026-02-06", "description": "telecom bill",
                     "confidence": 0.7})

    event = make_event()
    event.settlement_date = date(2026, 2, 10)
    event.status = "pending"
    image = ImageRef("image_05", "user_03", "request_03", "event_253", str(png))
    result = extract_image(make_client(transport, tmp_path), image, event, make_profile())

    assert result.amount == 822.05
    assert result.due_date == date(2026, 2, 6)
    assert "event settlement date: 2026-02-10" in sent["text"]
    assert "event status: pending" in sent["text"]
    assert "CUT-OFF / CROPPED DOCUMENTS" in sent["system"]


# ---------------------------------------------------------------------------
# toolbox
# ---------------------------------------------------------------------------

RATES = RateTable(rows=[
    (date(2024, 1, 15), "USD", "INR", 83.0),
    (date(2024, 2, 15), "USD", "INR", 84.0),
    (date(2024, 1, 15), "USD", "EUR", 0.92),
    (date(2024, 1, 15), "EUR", "ZAR", 20.0),
])


def test_rate_lookup_direct_inverse_and_via_usd():
    assert lookup_rate(RATES, "USD", "INR", date(2024, 2, 1)) == 83.0   # nearest <= on
    assert lookup_rate(RATES, "USD", "INR", date(2024, 3, 1)) == 84.0
    assert lookup_rate(RATES, "INR", "USD", date(2024, 2, 1)) == pytest.approx(1 / 83.0)
    assert lookup_rate(RATES, "INR", "EUR", date(2024, 2, 1)) == pytest.approx(0.92 / 83.0)
    assert lookup_rate(RATES, "XYZ", "INR", date(2024, 2, 1)) is None
    assert lookup_rate(RATES, "INR", "INR", date(2024, 2, 1)) == 1.0


def test_toolbox_dispatch():
    box = ToolBox(client=None, profile=make_profile(),
                  events_by_id={"event_253": make_event()},
                  images_by_id={"image_01": ImageRef("image_01", "user_03",
                                                     "request_03", "event_253", "/x.png")},
                  rates=RATES)
    import json
    assert json.loads(box.handle("get_rate", {"date": "2024-02-01", "from_ccy": "USD",
                                              "to_ccy": "INR"}))["rate"] == 83.0
    assert json.loads(box.handle("expand_evidence",
                                 {"event_id": "event_253"}))["event"]["category"] == "salary"
    assert "error" in json.loads(box.handle("expand_evidence", {"event_id": "nope"}))
    assert "error" in json.loads(box.handle("no_such_tool", {}))
    assert box.calls == ["get_rate", "expand_evidence", "expand_evidence", "no_such_tool"]
